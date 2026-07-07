"""데모 대시보드 백엔드 (로컬 FastAPI).

역할:
- 정적 UI(index.html) 서빙
- POST /convert  : 드롭된 scenario.json → 자연어 스텝 + Appium + Maestro 생성
- POST /run      : 시나리오로 병렬 "잡" N개 시작 (각 잡이 스텝을 순차 진행)
- GET  /jobs     : 모든 잡의 현재 상태(진행 스텝 + 스크린샷 URL) 폴링 → 타일 갱신
- GET  /shot/... : 스텝별 스크린샷(의사-라이브) 제공

설계: 이 백엔드는 발표자 노트북에서 로컬로 돈다. 무거운 추론은 에이전트(로컬 or AgentCore),
무거운 디바이스 실행은 Device Farm 이 담당. 대시보드 자체는 가볍다.

스크린샷은 "판단용"이 아니라 "표시용"(의사-라이브)이다 — LLM 실행 루프 없음.
데모 편의를 위해 스크린샷은 SVG 플레이스홀더를 즉석 생성한다(실기기 연동 시 이 함수만 교체).
"""
from __future__ import annotations

import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

# 어디서 실행하든(루트든 dashboard/ 안이든) sibling 모듈을 import 할 수 있도록
# 이 파일의 디렉토리를 경로에 추가.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_client import convert  # noqa: E402

app = FastAPI(title="QA Automation Demo Dashboard")

_HERE = Path(__file__).resolve().parent

# ---- 인메모리 잡 상태 (데모용) ---------------------------------------------
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


class ConvertReq(BaseModel):
    scenario: dict
    target: str = "appium"


class RunReq(BaseModel):
    scenario: dict
    steps: list[str]
    parallel: int = 3


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (_HERE / "index.html").read_text()


@app.get("/backend")
def backend():
    """UI 표시용: 지금 로컬 에이전트인지 AgentCore 인지."""
    import os
    return {"backend": os.environ.get("AGENT_BACKEND", "local")}


@app.get("/sample")
def sample():
    """샘플 scenario.json 반환 (드롭 없이 데모 시작용)."""
    import json
    p = _HERE.parent / "samples" / "happy_path_order.json"
    return JSONResponse(json.loads(p.read_text()))


@app.post("/convert")
def do_convert(req: ConvertReq):
    """단일 타깃 변환. UI가 steps/appium/maestro 각각 호출."""
    output = convert(req.scenario, req.target)
    return {"target": req.target, "output": output}


@app.post("/run")
def do_run(req: RunReq):
    """병렬 잡 시작. 각 잡은 독립 스레드에서 스텝을 순차 진행(디바이스 실행 시뮬레이션)."""
    job_ids = []
    for i in range(max(1, req.parallel)):
        jid = uuid.uuid4().hex[:8]
        device = ["Pixel 4", "Galaxy S20", "Galaxy S25", "Pixel 7", "Galaxy A54"][i % 5]
        with _lock:
            _jobs[jid] = {
                "id": jid,
                "device": f"{device} #{i + 1}",
                "steps": req.steps,
                "current": 0,
                "status": "running",
                "result": None,
            }
        threading.Thread(target=_drive_job, args=(jid,), daemon=True).start()
        job_ids.append(jid)
    return {"jobs": job_ids}


def _drive_job(jid: str) -> None:
    """잡 하나를 스텝별로 진행. 실제 디바이스 대신 시간차로 진행을 시뮬레이션.

    실기기 연동 시: 각 스텝 후 Device Farm/Appium 에서 실제 스크린샷을 받아 저장하도록
    이 루프만 교체하면 된다. 진행 모델(스텝 인덱스 + 상태)은 동일.
    """
    with _lock:
        steps = list(_jobs[jid]["steps"])
    for idx in range(len(steps)):
        time.sleep(1.2)  # 스텝 실행 시간 시뮬레이션
        with _lock:
            _jobs[jid]["current"] = idx + 1
    with _lock:
        _jobs[jid]["status"] = "passed"
        _jobs[jid]["result"] = "PASSED"


@app.get("/jobs")
def get_jobs():
    with _lock:
        return JSONResponse([_public_job(j) for j in _jobs.values()])


def _public_job(j: dict) -> dict:
    total = len(j["steps"])
    cur = j["current"]
    step_text = j["steps"][cur - 1] if 0 < cur <= total else ("대기 중" if cur == 0 else "완료")
    return {
        "id": j["id"],
        "device": j["device"],
        "current": cur,
        "total": total,
        "stepText": step_text,
        "status": j["status"],
        "result": j["result"],
        "shotUrl": f"/shot/{j['id']}/{cur}",
    }


@app.post("/reset")
def reset():
    with _lock:
        _jobs.clear()
    return {"ok": True}


# ---- 실제 Device Farm run 조회 (시뮬레이션 아님) ----------
@app.get("/df/runs")
def df_runs():
    """프로젝트의 실제 Device Farm run 상태/카운터."""
    try:
        from devicefarm_live import list_runs
        return {"runs": list_runs()}
    except Exception as e:  # 자격증명/설정 없을 때도 UI가 죽지 않게
        return JSONResponse({"error": str(e), "runs": []}, status_code=200)


@app.get("/df/video")
def df_video(arn: str):
    """완료된 run 에 영상이 있는지 여부만 반환(개수). 실제 재생은 /df/video_stream 으로.

    주의: Device Farm 이 주는 S3 presigned URL 을 브라우저에 직접 물리면
    만료/서명 문제로 403 이 나며 '재생되다 닫힘' 증상이 발생한다(관측됨).
    그래서 백엔드가 프록시로 스트리밍한다.
    """
    try:
        from devicefarm_live import run_video_urls
        urls = run_video_urls(arn)
        return {"count": len(urls), "streamUrl": f"/df/video_stream?arn={arn}&i=0" if urls else None}
    except Exception as e:
        return JSONResponse({"error": str(e), "count": 0, "streamUrl": None}, status_code=200)


@app.get("/df/video_stream")
def df_video_stream(arn: str, i: int = 0):
    """Device Farm 녹화 영상을 백엔드가 받아서 그대로 브라우저에 스트리밍(프록시).

    presigned S3 URL 을 브라우저에 직접 노출하지 않으므로 서명 만료/CORS/리다이렉트
    문제가 없다. 브라우저는 localhost 에서 안정적으로 받는다.
    """
    import requests
    from devicefarm_live import run_video_urls
    urls = run_video_urls(arn)
    if not urls or i >= len(urls):
        return JSONResponse({"error": "no video"}, status_code=404)
    r = requests.get(urls[i], stream=True)
    if not r.ok:
        return JSONResponse({"error": f"upstream {r.status_code}"}, status_code=502)
    return StreamingResponse(
        r.iter_content(chunk_size=64 * 1024),
        media_type="video/mp4",
        headers={"Content-Disposition": "inline; filename=devicefarm.mp4"},
    )


# ---- 실제 실행 (섹션 4: 시뮬레이션 아님) --------------------------------------
class RealRunReq(BaseModel):
    run_name: str = "dashboard-real-run"


@app.post("/real/start")
def real_start(req: RealRunReq):
    """실제 Device Farm run 을 백그라운드로 시작(패키징→업로드→실행→수집)."""
    from real_run import start_real_run
    return start_real_run(req.run_name)


@app.get("/real/status")
def real_status():
    """진행 중 실제 run 의 상태 + 완료 시 수집된 스크린샷 파일명."""
    from real_run import status
    return status()


@app.get("/real/shot/{name}")
def real_shot(name: str):
    """수집된 실제 스크린샷 PNG 서빙."""
    from real_run import shot_path
    p = shot_path(name)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return Response(content=p.read_bytes(), media_type="image/png")


# ---- 웹(Playwright + Browser Tool) 실행: 대시보드 Web 탭 ----------------------
class WebRunReq(BaseModel):
    recording: dict  # Chrome Recorder JSON


@app.get("/web/sample")
def web_sample():
    """샘플 Chrome Recorder JSON (TodoMVC)."""
    import json
    p = _HERE.parent / "web" / "samples" / "todomvc_recording.json"
    return JSONResponse(json.loads(p.read_text()))


@app.post("/web/start")
def web_start(req: WebRunReq):
    """Chrome Recorder JSON → Playwright 생성 → Browser Tool 실행(백그라운드)."""
    from web_run import start_web_run
    return start_web_run(req.recording)


@app.post("/web/convert")
def web_convert(req: WebRunReq):
    """Recorder JSON → Playwright 코드만 생성(미리보기용).

    주의: 배포된 AgentCore 런타임(runtime_app)은 playwright 타깃을 모르는 구버전일 수
    있으므로, 웹 변환은 항상 로컬 convert_scenario 를 직접 쓴다(web_run 과 동일 경로).
    """
    import sys as _sys
    from pathlib import Path as _P
    _agent = str(_P(__file__).resolve().parent.parent / "agent")
    if _agent not in _sys.path:
        _sys.path.insert(0, _agent)
    from convert import convert_scenario
    return {"target": "playwright", "output": convert_scenario(req.recording, "playwright")}


@app.post("/web/steps")
def web_steps(req: WebRunReq):
    """Recorder JSON → 자연어 스텝(로컬 변환, playwright 경로와 동일 이유로 로컬 고정)."""
    import sys as _sys
    from pathlib import Path as _P
    _agent = str(_P(__file__).resolve().parent.parent / "agent")
    if _agent not in _sys.path:
        _sys.path.insert(0, _agent)
    from convert import convert_scenario
    return {"output": convert_scenario(req.recording, "steps_web")}


@app.get("/web/status")
def web_status():
    from web_run import status
    return status()


@app.get("/web/shot/{name}")
def web_shot(name: str):
    from web_run import shot_path
    p = shot_path(name)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return Response(content=p.read_bytes(), media_type="image/png")


class ParallelReq(BaseModel):
    n: int = 5


@app.post("/web/parallel/start")
def web_parallel_start(req: ParallelReq):
    """직전 단일 web 실행의 결과(생성 스크립트 + 스크린샷)를 base 로, 변형 N개 병렬 실행."""
    from web_run import _GEN, _SHOTS
    import parallel_web
    if not _GEN.is_file():
        return JSONResponse({"error": "먼저 Web 단일 실행을 한 번 하세요(base 스크립트 필요)."}, status_code=200)
    base_code = _GEN.read_text()
    # 참고 스크린샷 하나(있으면).
    shot = None
    shots = sorted(_SHOTS.glob("step_*.png"))
    if shots:
        shot = shots[min(1, len(shots) - 1)].read_bytes()  # navigate 즈음
    return parallel_web.start(base_code, shot, max(1, min(req.n, 30)))


@app.get("/web/parallel/status")
def web_parallel_status():
    import parallel_web
    return parallel_web.status()


@app.get("/web/parallel/shot/{job}/{name}")
def web_parallel_shot(job: int, name: str):
    import parallel_web
    p = parallel_web.shot_path(job, name)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return Response(content=p.read_bytes(), media_type="image/png")


@app.get("/web/parallel/live/{job}")
def web_parallel_live(job: int):
    import parallel_web
    p = parallel_web.live_path(job)
    if not p:
        return JSONResponse({"error": "no frame"}, status_code=404)
    return Response(content=p.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/web/live")
def web_live():
    """준실시간 프리뷰 프레임(실행 중 ~1초마다 갱신). no-store 로 캐시 방지."""
    from web_run import live_frame
    p = live_frame()
    if not p:
        return JSONResponse({"error": "no frame"}, status_code=404)
    return Response(content=p.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/shot/{jid}/{step}")
def shot(jid: str, step: int) -> Response:
    """스텝별 스크린샷(의사-라이브). 데모용 SVG 플레이스홀더를 즉석 생성.

    실기기 연동 시 이 함수만 교체: Device Farm/Appium 스크린샷 PNG 를 반환.
    """
    with _lock:
        job = _jobs.get(jid)
        device = job["device"] if job else "?"
        total = len(job["steps"]) if job else 0
        text = job["steps"][step - 1] if job and 0 < step <= total else "…"
    svg = _phone_svg(device, step, total, text)
    return Response(content=svg, media_type="image/svg+xml")


def _phone_svg(device: str, step: int, total: int, text: str) -> str:
    """휴대폰 모양 SVG. 현재 스텝을 화면에 그려 '진행 중' 느낌을 준다."""
    safe = (text[:38] + "…") if len(text) > 38 else text
    safe = safe.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    done = step >= total and total > 0
    accent = "#22c55e" if done else "#38bdf8"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="220" height="380" viewBox="0 0 220 380">
  <rect x="10" y="10" width="200" height="360" rx="28" fill="#0b1220" stroke="#1e293b" stroke-width="3"/>
  <rect x="24" y="46" width="172" height="288" rx="10" fill="#0f172a"/>
  <circle cx="110" cy="28" r="4" fill="#334155"/>
  <text x="110" y="80" fill="#94a3b8" font-family="monospace" font-size="11" text-anchor="middle">{device}</text>
  <rect x="24" y="150" width="172" height="60" rx="8" fill="#111c34"/>
  <text x="110" y="175" fill="#e2e8f0" font-family="sans-serif" font-size="10" text-anchor="middle">step {step}/{total}</text>
  <text x="110" y="195" fill="{accent}" font-family="sans-serif" font-size="9" text-anchor="middle">{safe}</text>
  <rect x="24" y="320" width="{max(0, min(172, int(172 * (step / total)))) if total else 0}" height="6" rx="3" fill="{accent}"/>
  <text x="110" y="352" fill="{accent}" font-family="monospace" font-size="10" text-anchor="middle">{'PASSED ✓' if done else 'running…'}</text>
</svg>"""
