"""Demo dashboard backend (local FastAPI).

Responsibilities:
- Serve the static UI (index.html)
- POST /convert  : dropped scenario.json -> generate natural-language steps + Appium + Maestro
- POST /run      : start N parallel "jobs" from a scenario (each job advances through steps sequentially)
- GET  /jobs     : poll the current state of all jobs (current step + screenshot URL) -> refresh tiles
- GET  /shot/... : serve per-step screenshots (pseudo-live)

Design: this backend runs locally on the presenter's laptop. Heavy inference is handled
by the agent (local or AgentCore), and heavy device execution by Device Farm. The
dashboard itself is lightweight.

Screenshots are for "display" (pseudo-live), not for "decision-making" -- there is no
LLM execution loop. For demo convenience, screenshots are generated on the fly as SVG
placeholders (to integrate a real device, replace only this function).
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

# Add this file's directory to the path so sibling modules can be imported regardless of
# where the process is launched (repo root or inside dashboard/).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_client import convert  # noqa: E402

app = FastAPI(title="QA Automation Demo Dashboard")

_HERE = Path(__file__).resolve().parent

# ---- In-memory job state (for the demo) ------------------------------------
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
    """For UI display: whether the current backend is the local agent or AgentCore."""
    import os
    return {"backend": os.environ.get("AGENT_BACKEND", "local")}


@app.get("/sample")
def sample():
    """Return the sample scenario.json (to start the demo without dropping a file)."""
    import json
    p = _HERE.parent / "samples" / "happy_path_order.json"
    return JSONResponse(json.loads(p.read_text()))


@app.post("/convert")
def do_convert(req: ConvertReq):
    """Single-target conversion. The UI calls this separately for steps/appium/maestro."""
    output = convert(req.scenario, req.target)
    return {"target": req.target, "output": output}


@app.post("/run")
def do_run(req: RunReq):
    """Start parallel jobs. Each job advances through steps sequentially on its own thread (simulating device execution)."""
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
    """Advance a single job step by step. Simulates progress with time delays instead of a real device.

    To integrate a real device: replace only this loop so that after each step it fetches
    and stores a real screenshot from Device Farm/Appium. The progress model (step index +
    status) stays the same.
    """
    with _lock:
        steps = list(_jobs[jid]["steps"])
    for idx in range(len(steps)):
        time.sleep(1.2)  # simulate step execution time
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
    step_text = j["steps"][cur - 1] if 0 < cur <= total else ("Waiting" if cur == 0 else "Done")
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


# ---- Query real Device Farm runs (not a simulation) ----------
@app.get("/df/runs")
def df_runs():
    """Real Device Farm run status/counters for the project."""
    try:
        from devicefarm_live import list_runs
        return {"runs": list_runs()}
    except Exception as e:  # keep the UI alive even when credentials/config are missing
        return JSONResponse({"error": str(e), "runs": []}, status_code=200)


@app.get("/df/video")
def df_video(arn: str):
    """Return only whether a completed run has a video (count). Actual playback goes through /df/video_stream.

    Note: pointing the browser directly at the S3 presigned URL from Device Farm causes
    403s due to expiry/signature issues, producing a "plays then closes" symptom. The
    backend therefore proxies the stream instead.
    """
    try:
        from devicefarm_live import run_video_urls
        urls = run_video_urls(arn)
        return {"count": len(urls), "streamUrl": f"/df/video_stream?arn={arn}&i=0" if urls else None}
    except Exception as e:
        return JSONResponse({"error": str(e), "count": 0, "streamUrl": None}, status_code=200)


@app.get("/df/video_stream")
def df_video_stream(arn: str, i: int = 0):
    """The backend fetches the Device Farm recording and streams it straight to the browser (proxy).

    Because the presigned S3 URL is never exposed to the browser directly, there are no
    signature-expiry/CORS/redirect issues. The browser receives it reliably from localhost.
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


# ---- Real execution (section 4: not a simulation) --------------------------------------
class RealRunReq(BaseModel):
    run_name: str = "dashboard-real-run"


@app.post("/real/start")
def real_start(req: RealRunReq):
    """Start a real Device Farm run in the background (package -> upload -> run -> collect)."""
    from real_run import start_real_run
    return start_real_run(req.run_name)


@app.get("/real/status")
def real_status():
    """Status of the in-progress real run + collected screenshot file names once complete."""
    from real_run import status
    return status()


@app.get("/real/shot/{name}")
def real_shot(name: str):
    """Serve a collected real screenshot PNG."""
    from real_run import shot_path
    p = shot_path(name)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return Response(content=p.read_bytes(), media_type="image/png")


# ---- Web (Playwright + Browser Tool) execution: dashboard Web tab ----------------------
class WebRunReq(BaseModel):
    recording: dict  # Chrome Recorder JSON


@app.get("/web/sample")
def web_sample():
    """Sample Chrome Recorder JSON (TodoMVC)."""
    import json
    p = _HERE.parent / "web" / "samples" / "todomvc_recording.json"
    return JSONResponse(json.loads(p.read_text()))


@app.post("/web/start")
def web_start(req: WebRunReq):
    """Chrome Recorder JSON -> generate Playwright -> run on Browser Tool (background)."""
    from web_run import start_web_run
    return start_web_run(req.recording)


@app.post("/web/convert")
def web_convert(req: WebRunReq):
    """Recorder JSON -> generate Playwright code only (for preview).

    Note: the deployed AgentCore runtime (runtime_app) may be an older version that does
    not know the playwright target, so web conversion always uses local convert_scenario
    directly (same path as web_run).
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
    """Recorder JSON -> natural-language steps (local conversion; pinned to local for the same reason as the playwright path)."""
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
    """Using the previous single web run's result (generated script + screenshots) as the base, run N variations in parallel."""
    from web_run import _GEN, _SHOTS
    import parallel_web
    if not _GEN.is_file():
        return JSONResponse({"error": "Run a single Web execution first (a base script is required)."}, status_code=200)
    base_code = _GEN.read_text()
    # One reference screenshot (if any).
    shot = None
    shots = sorted(_SHOTS.glob("step_*.png"))
    if shots:
        shot = shots[min(1, len(shots) - 1)].read_bytes()  # around the navigate step
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
    """Near-real-time preview frame (refreshed roughly every second during a run). no-store prevents caching."""
    from web_run import live_frame
    p = live_frame()
    if not p:
        return JSONResponse({"error": "no frame"}, status_code=404)
    return Response(content=p.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/shot/{jid}/{step}")
def shot(jid: str, step: int) -> Response:
    """Per-step screenshot (pseudo-live). Generates a demo SVG placeholder on the fly.

    To integrate a real device, replace only this function: return a Device Farm/Appium
    screenshot PNG.
    """
    with _lock:
        job = _jobs.get(jid)
        device = job["device"] if job else "?"
        total = len(job["steps"]) if job else 0
        text = job["steps"][step - 1] if job and 0 < step <= total else "…"
    svg = _phone_svg(device, step, total, text)
    return Response(content=svg, media_type="image/svg+xml")


def _phone_svg(device: str, step: int, total: int, text: str) -> str:
    """Phone-shaped SVG. Draws the current step on the screen to convey an 'in progress' feel."""
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
