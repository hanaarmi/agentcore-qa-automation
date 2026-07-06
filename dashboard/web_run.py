"""대시보드 Web 탭용: Chrome Recorder JSON → Playwright 생성 → Browser Tool 실행.

모바일의 real_run.py 와 대칭. 백그라운드 스레드로 실행하고 상태/스크린샷을 인메모리 추적.
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
_LIVE = _SHOTS / "_live.png"  # 준실시간 프리뷰 프레임(실행 중 ~1초마다 갱신)


def start_web_run(recording: dict) -> dict:
    """Chrome Recorder JSON 을 받아 변환→실행을 백그라운드로 시작."""
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
        # 1) Recorder JSON -> Playwright 스크립트 (모바일과 같은 변환 에이전트).
        _set(status="RUNNING", phase="generating (Opus)")
        from convert import convert_scenario
        code = convert_scenario(recording, "playwright")
        _GEN.parent.mkdir(parents=True, exist_ok=True)
        _GEN.write_text(code + "\n")

        # 2) Browser Tool 에서 실행. 준실시간 프리뷰 프레임(_LIVE)을 ~1초마다 갱신.
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
    """준실시간 프리뷰 최신 프레임 경로(있으면)."""
    return _LIVE if _LIVE.is_file() else None
