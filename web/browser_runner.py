"""AgentCore Browser Tool 러너.

생성된 Playwright 스크립트(web/out/*.py 의 async def run(page))를 AWS 관리형
클라우드 브라우저(AgentCore Browser Tool)에서 실행한다. Chromium 을 로컬에 안 띄운다 —
관리형 브라우저에 CDP(WebSocket)로 붙는다.

모바일(Device Farm) 경로와 대칭:
  - 모바일: 실기기(관리형) ← Appium
  - 웹    : 클라우드 브라우저(관리형) ← Playwright
무거운 실행은 관리형에 오프로드, 우리는 오케스트레이션만(D-005/부하 회피).

사용:
    python web/browser_runner.py web/out/todomvc_playwright.py [--shots web/out/shots]
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
from pathlib import Path

REGION = os.environ.get("AGENTCORE_REGION", "us-west-2")


def _load_run_fn(script_path: Path):
    """생성된 스크립트에서 async def run(page) 를 로드."""
    spec = importlib.util.spec_from_file_location("gen_pw_test", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "run"):
        raise SystemExit(f"{script_path} 에 async def run(page) 가 없습니다.")
    return mod.run


async def _frame_grabber(page, live_path: Path, stop: "asyncio.Event") -> None:
    """실행 중 ~1초마다 현재 화면을 live.png 로 덮어써 준실시간 프리뷰를 만든다.

    AgentCore 라이브 뷰는 DCV 스트림이라 iframe 으로 못 띄운다(501). DCV Web SDK 없이
    순수 HTML 대시보드에서 '지켜보기'를 구현하는 가장 견고한 방법 = 스크린샷 폴링.
    """
    while not stop.is_set():
        try:
            await page.screenshot(path=str(live_path))
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def _drive(script_path: Path, shot_dir: Path, live_path: Path | None = None) -> dict:
    from bedrock_agentcore.tools.browser_client import browser_session
    from playwright.async_api import async_playwright

    shot_dir.mkdir(parents=True, exist_ok=True)
    os.environ["WEB_SHOT_DIR"] = str(shot_dir)
    run_fn = _load_run_fn(script_path)

    result = {"status": "running", "shots": [], "error": None}
    with browser_session(REGION) as client:
        ws_url, headers = client.generate_ws_headers()
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(ws_url, headers=headers)
            grab_task = None
            stop = asyncio.Event()
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = context.pages[0] if context.pages else await context.new_page()
                # 준실시간 프리뷰용 프레임 그래버 시작.
                if live_path is not None:
                    live_path.parent.mkdir(parents=True, exist_ok=True)
                    grab_task = asyncio.create_task(_frame_grabber(page, live_path, stop))
                await run_fn(page)
                result["status"] = "passed"
            except Exception as e:  # noqa: BLE001
                result["status"] = "failed"
                result["error"] = str(e)
            finally:
                stop.set()
                if grab_task is not None:
                    try:
                        await grab_task
                    except Exception:  # noqa: BLE001
                        pass
                await browser.close()

    result["shots"] = sorted(p.name for p in shot_dir.glob("*.png"))
    return result


def run_script(script_path: Path, shot_dir: Path, live_path: Path | None = None) -> dict:
    return asyncio.run(_drive(script_path, shot_dir, live_path))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("script", type=Path, help="생성된 Playwright 스크립트")
    ap.add_argument("--shots", type=Path, default=Path("web/out/shots"))
    args = ap.parse_args()
    if not args.script.is_file():
        raise SystemExit(f"not found: {args.script}")
    res = run_script(args.script, args.shots)
    print("status:", res["status"])
    if res["error"]:
        print("error:", res["error"])
    print("shots:", len(res["shots"]))
    return 0 if res["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
