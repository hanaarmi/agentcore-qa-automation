"""대시보드용: 변형 N개 생성 → N개 runtime 병렬 invoke (각 runtime이 자기 Browser Tool 구동).

핵심(사용자 설계): 로컬에서 Browser Tool 을 직접 돌리지 않는다. 대신 배포된 AgentCore
Runtime 을 N번 병렬 invoke 하고, 각 invoke 가 자기 microVM 안에서 자기 Browser Tool 세션을
연다. 오케스트레이션 부하가 서버리스로 분산 → 로컬 박스 부하 없음(c5.metal 교훈).

로컬 대시보드는 (1) 변형 생성 요청, (2) N개 invoke, (3) 결과 폴링만 한다.
"""
from __future__ import annotations

import base64
import json
import sys
import threading
from pathlib import Path

import boto3

_ROOT = Path(__file__).resolve().parent.parent
for p in (_ROOT / "agent", _ROOT / "web"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# 환경변수로 주입(공개 저장소에 계정ID/ARN 하드코딩 금지).
#   AGENTCORE_REGION : 런타임 리전 (기본 us-west-2)
#   AGENTCORE_ARN    : 배포된 AgentCore Runtime ARN (필수)
import os
REGION = os.environ.get("AGENTCORE_REGION", "us-west-2")
RUNTIME_ARN = os.environ.get("AGENTCORE_ARN", "")

_OUT = _ROOT / "web" / "out" / "parallel"
_state: dict = {"phase": "idle", "n": 0, "error": None, "runId": 0}
_jobs: dict[int, dict] = {}
_lock = threading.Lock()


def _set_job(i: int, **kw):
    with _lock:
        _jobs.setdefault(i, {"id": i})
        _jobs[i].update(kw)


def start(base_code: str, screenshot: bytes | None, n: int) -> dict:
    # 주의: _public() 은 내부에서 _lock 을 다시 잡는다. threading.Lock 은 재진입 불가 →
    # _lock 을 잡은 채로 _public() 을 부르면 데드락(관측됨: status 무한 대기, CPU 0%).
    # 따라서 lock 안에서는 상태만 바꾸고, _public() 은 lock 밖에서 호출한다.
    with _lock:
        busy = _state["phase"] in ("generating", "running")
        if not busy:
            # 새 실행: runId 증가 + 상태/잡 완전 초기화(옛 done 결과가 남지 않도록).
            _state.update(phase="generating", n=n, error=None, runId=_state["runId"] + 1)
            _jobs.clear()
    if busy:
        return _public()
    threading.Thread(target=_drive, args=(base_code, screenshot, n), daemon=True).start()
    return _public()


def _drive(base_code: str, screenshot: bytes | None, n: int) -> None:
    try:
        from variations import brainstorm_scenarios
        # 1단계(로컬, 1회): 스크린샷 기반으로 서로 다른 시나리오 N개 브레인스토밍.
        scenarios = brainstorm_scenarios(base_code, screenshot, n)
        with _lock:
            _state["phase"] = "running"
            for i, sc in enumerate(scenarios):
                _jobs[i] = {"id": i, "status": "starting", "result": None, "shots": [],
                            "error": None, "title": sc["title"], "desc": sc["desc"]}
        # 2·3단계(runtime N개 병렬): 각 runtime 이 brief 받아 스크립트 생성(LLM)+실행.
        # 로컬은 invoke 만 — 동기 invoke 를 N개 동시에 대기시키려고 스레드 사용(부하 아님, 대기).
        threads = [threading.Thread(target=_invoke_one, args=(i, scenarios[i], base_code),
                                    daemon=True) for i in range(len(scenarios))]
        for t in threads:
            t.start()
    except Exception as e:  # noqa: BLE001
        with _lock:
            _state.update(phase="error", error=str(e))


def _invoke_one(i: int, scenario: dict, base_code: str) -> None:
    """runtime 1회 invoke → scenario_run (runtime 안에서 스크립트 생성 + 실행)."""
    _set_job(i, status="running")
    shot_dir = _OUT / f"job_{i:02d}"
    shot_dir.mkdir(parents=True, exist_ok=True)
    try:
        client = boto3.client("bedrock-agentcore", region_name=REGION)
        payload = json.dumps({
            "action": "scenario_run",
            "brief": scenario["brief"],
            "base_script": base_code,
            "label": f"var-{i}",
        }).encode()
        r = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN, qualifier="DEFAULT", payload=payload,
            contentType="application/json", accept="application/json",
            runtimeSessionId=f"parallel-variation-{i:02d}-" + "0" * 16,  # 33+ chars, per-invoke
        )
        d = json.loads(r["response"].read())
        names = []
        for sh in d.get("shots", []):
            (shot_dir / sh["name"]).write_bytes(base64.b64decode(sh["b64"]))
            names.append(sh["name"])
        _set_job(i, status="completed",
                 result=("PASSED" if d.get("status") == "passed" else "FAILED"),
                 error=d.get("error"), shots=sorted(names))
    except Exception as e:  # noqa: BLE001
        _set_job(i, status="completed", result="FAILED", error=str(e))


def _public() -> dict:
    with _lock:
        # 모든 잡이 끝났으면 phase 를 실제로 "done" 으로 확정(표시용 계산만 하면 _state 는
        # 계속 "running" 이라 다음 start() 가 busy 로 판단해 새 실행이 시작 안 되는 버그).
        if (_state["phase"] == "running" and _jobs
                and all(j.get("status") == "completed" for j in _jobs.values())):
            _state["phase"] = "done"
        phase = _state["phase"]; n = _state["n"]; err = _state["error"]; run_id = _state["runId"]
        jobs = [dict(j) for _, j in sorted(_jobs.items())]
    return {"phase": phase, "n": n, "error": err, "runId": run_id, "jobs": jobs}


def status() -> dict:
    return _public()


def shot_path(job: int, name: str) -> Path | None:
    p = _OUT / f"job_{job:02d}" / name
    return p if p.is_file() else None


def live_path(job: int) -> Path | None:
    # runtime 실행 방식에선 실행 중 라이브 프레임을 로컬에서 못 받는다(완료 후 스크린샷만).
    return None
