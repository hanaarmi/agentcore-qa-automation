"""Real Device Farm run orchestration for the dashboard (not a simulation).

'Start run' actually performs: package the Appium test -> upload -> schedule_run ->
poll status -> after completion, collect screenshots + video artifacts. Progress is
tracked in memory for the dashboard to poll.

Device Farm does not provide screenshots in real time during a run, so: during a run
only the run status is shown; after completion the collected per-step screenshots are
shown as tiles (a filmstrip), with the video behind a button.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import boto3

_ROOT = Path(__file__).resolve().parent.parent
_INFRA = _ROOT / "infra"
_AGENT = _ROOT / "agent"
for _p in (_INFRA, _AGENT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Reuse the proven run helpers and config resolver from infra.
import devicefarm_run as dfr  # noqa: E402
from df_config import resolve_config  # noqa: E402

_state: dict = {"active": None}
_lock = threading.Lock()

# Store screenshots collected after completion so the dashboard can serve them.
_SHOT_DIR = _ROOT / "artifacts" / "dashboard_run"


def _cfg():
    # Auto-configure in order: config.json -> env -> lookup of the CDK-created project/pool names.
    return resolve_config()


# Mobile test package (zip). The repo only ships a sample scenario JSON; the zip is a
# build artifact and is not committed. If missing, convert the sample scenario to Appium
# and build it on the fly (removes a manual step).
_PKG_PATH = _ROOT / "out" / "appium_pkg.zip"
_SAMPLE_SCENARIO = _ROOT / "samples" / "happy_path_order.json"


def _ensure_pkg() -> Path:
    """If out/appium_pkg.zip is missing, build it: sample scenario -> Appium conversion -> package."""
    if _PKG_PATH.is_file():
        return _PKG_PATH

    import json
    from convert import convert_scenario  # agent/convert.py (local conversion)
    from package_appium import build      # infra/package_appium.py

    scenario = json.loads(_SAMPLE_SCENARIO.read_text())
    appium_code = convert_scenario(scenario, "appium")

    out_py = _ROOT / "out" / "happy_path_order_appium.py"
    out_py.parent.mkdir(parents=True, exist_ok=True)
    out_py.write_text(appium_code)
    build(out_py, _PKG_PATH)
    return _PKG_PATH


def start_real_run(run_name: str) -> dict:
    """Start a real run in the background. If a run is already active, return it."""
    with _lock:
        if _state["active"] and _state["active"]["status"] in ("STARTING", "RUNNING"):
            return _public()
        _state["active"] = {
            "name": run_name,
            "status": "STARTING",
            "phase": "packaging",
            "runArn": None,
            "result": None,
            "shots": [],
            "hasVideo": False,
            "error": None,
        }
    threading.Thread(target=_drive_real, args=(run_name,), daemon=True).start()
    return _public()


def _set(**kw):
    with _lock:
        if _state["active"]:
            _state["active"].update(kw)


def _drive_real(run_name: str) -> None:
    try:
        cfg = _cfg()
        client = boto3.client("devicefarm", region_name=cfg["region"])

        apk = _ROOT / "assets" / "deliveryapp-debug.apk"
        spec = _INFRA / "testspec_appium_python.yml"

        # If the test package is missing, build it on the fly from the sample scenario (removes the manual packaging step).
        _set(phase="packaging tests", status="RUNNING")
        pkg = _ensure_pkg()

        _set(phase="uploading app")
        app_arn = dfr.create_and_wait_upload(client, cfg["projectArn"], apk, "ANDROID_APP")
        _set(phase="uploading tests")
        test_arn = dfr.create_and_wait_upload(
            client, cfg["projectArn"], pkg, "APPIUM_PYTHON_TEST_PACKAGE"
        )
        _set(phase="uploading spec")
        spec_arn = dfr.create_and_wait_upload(
            client, cfg["projectArn"], spec, "APPIUM_PYTHON_TEST_SPEC"
        )

        _set(phase="scheduling")
        run = dfr.schedule_and_wait_run(
            client, cfg["projectArn"], cfg["devicePoolArn"],
            app_arn, test_arn, "APPIUM_PYTHON", run_name, spec_arn,
        )
        # schedule_and_wait_run blocks until COMPLETED, so reaching here means it is done.
        _set(phase="collecting", runArn=run["arn"], result=run.get("result"))

        # Collect screenshots + video.
        _SHOT_DIR.mkdir(parents=True, exist_ok=True)
        shots = _collect_screenshots(client, run["arn"])
        has_video = bool(_video_urls(client, run["arn"]))
        _set(status="COMPLETED", phase="done", shots=shots, hasVideo=has_video,
             result=run.get("result"))
    except Exception as e:  # noqa: BLE001
        _set(status="ERROR", error=str(e))


def _collect_screenshots(client, run_arn: str) -> list[str]:
    """Download the run's screenshots (including the step_*.png files left by the test). Returns the list of file names."""
    import requests
    names = []
    for atype in ("SCREENSHOT", "FILE"):
        arts = client.list_artifacts(arn=run_arn, type=atype)["artifacts"]
        for a in arts:
            if a.get("extension") != "png" or not a.get("url"):
                continue
            fn = f"{a['name']}-{a['arn'].split('/')[-1][:8]}.png".replace("/", "_")
            r = requests.get(a["url"])
            if r.ok:
                (_SHOT_DIR / fn).write_bytes(r.content)
                names.append(fn)
    names.sort()
    return names


def _video_urls(client, run_arn: str) -> list[str]:
    urls = []
    for a in client.list_artifacts(arn=run_arn, type="FILE")["artifacts"]:
        if a.get("type") == "VIDEO" and a.get("url"):
            urls.append(a["url"])
    return urls


def _public() -> dict:
    with _lock:
        s = _state["active"]
        return dict(s) if s else {"status": "IDLE"}


def status() -> dict:
    return _public()


def shot_path(name: str) -> Path | None:
    p = _SHOT_DIR / name
    return p if p.is_file() else None
