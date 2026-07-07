"""Dashboard Web tab: Chrome Recorder JSON -> generate Playwright -> run on Browser Tool.

Symmetric to the mobile real_run.py. Runs on a background thread and tracks
status/screenshots in memory.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for p in (_ROOT / "agent", _ROOT / "web"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

_state: dict = {"active": None}
_lock = threading.Lock()

_GEN = _ROOT / "web" / "out" / "dashboard_web_test.py"
_SHOTS = _ROOT / "web" / "out" / "dashboard_shots"
_LIVE = _SHOTS / "_live.png"  # near-real-time preview frame (refreshed roughly every second during a run)


def start_web_run(recording: dict) -> dict:
    """Take Chrome Recorder JSON and start conversion -> execution in the background."""
    with _lock:
        if _state["active"] and _state["active"]["status"] in ("STARTING", "RUNNING"):
            return _public()
        _state["active"] = {
            "status": "STARTING", "phase": "generating",
            "result": None, "shots": [], "error": None, "live": False,
        }
    threading.Thread(target=_drive, args=(recording,), daemon=True).start()
    return _public()


def _set(**kw):
    with _lock:
        if _state["active"]:
            _state["active"].update(kw)


def _drive(recording: dict) -> None:
    try:
        # 1) Recorder JSON -> Playwright script (same conversion agent as mobile).
        _set(status="RUNNING", phase="generating (Opus)")
        from convert import convert_scenario
        code = convert_scenario(recording, "playwright")
        _GEN.parent.mkdir(parents=True, exist_ok=True)
        _GEN.write_text(code + "\n")

        # 2) Run on the Browser Tool. Refresh the near-real-time preview frame (_LIVE) roughly every second.
        _set(phase="running on Browser Tool", live=True)
        import browser_runner
        res = browser_runner.run_script(_GEN, _SHOTS, live_path=_LIVE)

        _set(status="COMPLETED", phase="done", live=False,
             result=("PASSED" if res["status"] == "passed" else "FAILED"),
             shots=res["shots"], error=res.get("error"))
    except Exception as e:  # noqa: BLE001
        _set(status="ERROR", error=str(e))


def _public() -> dict:
    with _lock:
        s = _state["active"]
        return dict(s) if s else {"status": "IDLE"}


def status() -> dict:
    return _public()


def shot_path(name: str) -> Path | None:
    p = _SHOTS / name
    return p if p.is_file() else None


def live_frame() -> Path | None:
    """Path to the latest near-real-time preview frame (if any)."""
    return _LIVE if _LIVE.is_file() else None
