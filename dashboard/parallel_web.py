"""Dashboard: generate N variations -> invoke N runtimes in parallel (each runtime drives its own Browser Tool).

Key design: the Browser Tool is not run locally. Instead, the deployed AgentCore
Runtime is invoked N times in parallel, and each invocation opens its own Browser
Tool session inside its own microVM. Orchestration load is spread across the
serverless fleet, so the local machine bears no load.

The local dashboard only (1) requests variation generation, (2) issues the N
invocations, and (3) polls for results.
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

# Injected via environment variables (never hard-code account IDs/ARNs in a public repo).
#   AGENTCORE_REGION : runtime region (default us-west-2)
#   AGENTCORE_ARN    : deployed AgentCore Runtime ARN (required)
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
    # Note: _public() reacquires _lock internally. threading.Lock is not reentrant, so
    # calling _public() while holding _lock deadlocks (status hangs indefinitely, CPU 0%).
    # Therefore only mutate state inside the lock and call _public() outside the lock.
    with _lock:
        busy = _state["phase"] in ("generating", "running")
        if not busy:
            # New run: increment runId and fully reset state/jobs (so stale done results do not linger).
            _state.update(phase="generating", n=n, error=None, runId=_state["runId"] + 1)
            _jobs.clear()
    if busy:
        return _public()
    threading.Thread(target=_drive, args=(base_code, screenshot, n), daemon=True).start()
    return _public()


def _drive(base_code: str, screenshot: bytes | None, n: int) -> None:
    try:
        from variations import brainstorm_scenarios
        # Step 1 (local, once): brainstorm N distinct scenarios based on the screenshot.
        scenarios = brainstorm_scenarios(base_code, screenshot, n)
        with _lock:
            _state["phase"] = "running"
            for i, sc in enumerate(scenarios):
                _jobs[i] = {"id": i, "status": "starting", "result": None, "shots": [],
                            "error": None, "title": sc["title"], "desc": sc["desc"]}
        # Steps 2-3 (N runtimes in parallel): each runtime takes a brief, generates a script (LLM), and runs it.
        # The local side only invokes -- threads are used to await N synchronous invokes concurrently (waiting, not load).
        threads = [threading.Thread(target=_invoke_one, args=(i, scenarios[i], base_code),
                                    daemon=True) for i in range(len(scenarios))]
        for t in threads:
            t.start()
    except Exception as e:  # noqa: BLE001
        with _lock:
            _state.update(phase="error", error=str(e))


def _invoke_one(i: int, scenario: dict, base_code: str) -> None:
    """Invoke the runtime once -> scenario_run (script generation + execution inside the runtime)."""
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
        # Once all jobs finish, actually commit phase to "done". If we only computed it for
        # display, _state would stay "running", so the next start() would treat it as busy
        # and never begin a new run.
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
    # With the runtime execution model, live frames cannot be received locally during a run (only screenshots after completion).
    return None
