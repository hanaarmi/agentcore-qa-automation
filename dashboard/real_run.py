"""대시보드용 실제 Device Farm 실행 오케스트레이션 (시뮬레이션 아님).

'실행 시작' → 실제로: Appium 테스트 패키징 → 업로드 → schedule_run → 상태 폴링 →
완료 후 스크린샷 + 영상 아티팩트 수집. 진행 상태를 인메모리로 추적해 대시보드가 폴링.

Device Farm 은 실행 '중' 스크린샷을 실시간 제공하지 않는다 → 진행 중엔 run 상태만,
완료 후 수집된 스텝별 스크린샷을 타일(필름스트립)로, 영상은 버튼으로.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import boto3

_ROOT = Path(__file__).resolve().parent.parent
_INFRA = _ROOT / "infra"
if str(_INFRA) not in sys.path:
    sys.path.insert(0, str(_INFRA))

# infra 의 검증된 실행 함수 + 설정 리졸버 재사용.
import devicefarm_run as dfr  # noqa: E402
from df_config import resolve_config  # noqa: E402

_state: dict = {"active": None}
_lock = threading.Lock()

# 완료 후 수집한 스크린샷을 대시보드가 서빙할 수 있도록 저장.
_SHOT_DIR = _ROOT / "artifacts" / "dashboard_run"


def _cfg():
    # config.json → env → CDK 가 만든 프로젝트/풀 이름 조회 순으로 자동 구성.
    return resolve_config()


def start_real_run(run_name: str) -> dict:
    """실제 run 을 백그라운드로 시작. 이미 활성 run 이 있으면 그걸 반환."""
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
        pkg = _ROOT / "out/appium_pkg.zip"
        spec = _INFRA / "testspec_appium_python.yml"

        _set(phase="uploading app", status="RUNNING")
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
        # schedule_and_wait_run 은 COMPLETED 까지 블로킹 → 여기 오면 완료.
        _set(phase="collecting", runArn=run["arn"], result=run.get("result"))

        # 스크린샷 + 영상 수집.
        _SHOT_DIR.mkdir(parents=True, exist_ok=True)
        shots = _collect_screenshots(client, run["arn"])
        has_video = bool(_video_urls(client, run["arn"]))
        _set(status="COMPLETED", phase="done", shots=shots, hasVideo=has_video,
             result=run.get("result"))
    except Exception as e:  # noqa: BLE001
        _set(status="ERROR", error=str(e))


def _collect_screenshots(client, run_arn: str) -> list[str]:
    """run 의 스크린샷(우리 테스트가 남긴 step_*.png 포함) 다운로드. 파일명 리스트 반환."""
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
