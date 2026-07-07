"""AgentCore Browser Tool runner.

Runs a generated Playwright script (the async def run(page) in web/out/*.py) on
the AWS managed cloud browser (AgentCore Browser Tool). It does not launch a local
Chromium; it connects to the managed browser over CDP (WebSocket).

Symmetric with the mobile (Device Farm) path:
  - mobile: real device (managed)   <- Appium
  - web   : cloud browser (managed) <- Playwright
Heavy execution is offloaded to the managed service; we only orchestrate (avoiding
local load).

Usage:
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
    """Load async def run(page) from the generated script."""
    spec = importlib.util.spec_from_file_location("gen_pw_test", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "run"):
        raise SystemExit(f"{script_path} does not define async def run(page).")
    return mod.run


async def _frame_grabber(page, live_path: Path, stop: "asyncio.Event") -> None:
    """Overwrite live.png with the current screen roughly every second to provide a near-real-time preview.

    The AgentCore live view is a DCV stream and cannot be embedded in an iframe
    (501). Without the DCV Web SDK, screenshot polling is the most robust way to
    implement a "watch" view in a plain HTML dashboard.
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
                # Start the frame grabber for the near-real-time preview.
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
    ap.add_argument("script", type=Path, help="generated Playwright script")
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
