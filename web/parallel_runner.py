"""Parallel runner - runs N independent Browser Tool sessions concurrently.

Each variation script runs on one thread = one session (the SDK has no async
client, so threads are used). Each session is an independent microVM (fully
isolated), so there is no load on our box (all execution happens on the AWS side).
Sessions must be cleaned up with stop() to avoid idle billing.

Status/screenshots are tracked in memory per job for the dashboard to poll.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

REGION = "us-west-2"
SESSION_TIMEOUT = 300  # kept short for the demo

# Strip code fences (in case the LLM wraps output in ```python ... ```).
_FENCE = re.compile(r"^```[a-zA-Z]*\n|\n```$")


def _clean(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        code = re.sub(r"^```[a-zA-Z]*\n", "", code)
        code = re.sub(r"\n```$", "", code)
    return code


def _load_run_fn(code: str):
    ns: dict = {}
    exec(compile(_clean(code), "<variation>", "exec"), ns)  # noqa: S102
    fn = ns.get("run")
    if fn is None:
        raise RuntimeError("variation does not define async def run(page)")
    return fn


class ParallelJobs:
    """Manages parallel job state."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[int, dict] = {}
        self.lock = threading.Lock()

    def _set(self, i: int, **kw):
        with self.lock:
            self.jobs[i].setdefault("id", i)
            self.jobs[i].update(kw)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [dict(j) for _, j in sorted(self.jobs.items())]

    def start(self, scripts: list[str]) -> None:
        with self.lock:
            self.jobs = {i: {"id": i, "status": "starting", "phase": "queued",
                             "result": None, "shots": [], "live": False, "error": None}
                         for i in range(len(scripts))}
        for i, code in enumerate(scripts):
            threading.Thread(target=self._run_one, args=(i, code), daemon=True).start()

    def _run_one(self, i: int, code: str) -> None:
        import asyncio
        from bedrock_agentcore.tools.browser_client import BrowserClient
        from playwright.async_api import async_playwright

        shot_dir = self.out_dir / f"job_{i:02d}"
        shot_dir.mkdir(parents=True, exist_ok=True)
        live_path = shot_dir / "_live.png"

        async def drive():
            run_fn = _load_run_fn(code)
            client = BrowserClient(REGION)
            self._set(i, status="running", phase="starting session")
            try:
                client.start(identifier="aws.browser.v1", name=f"var-{i}",
                             session_timeout_seconds=SESSION_TIMEOUT)
                ws_url, headers = client.generate_ws_headers()
                self._set(i, phase="connected", live=True)
                async with async_playwright() as pw:
                    browser = await pw.chromium.connect_over_cdp(ws_url, headers=headers)
                    stop = asyncio.Event()
                    grab = None
                    try:
                        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                        import os as _os
                        _os.environ["WEB_SHOT_DIR"] = str(shot_dir)

                        async def grabber():
                            while not stop.is_set():
                                try:
                                    await page.screenshot(path=str(live_path))
                                except Exception:  # noqa: BLE001
                                    pass
                                try:
                                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                                except asyncio.TimeoutError:
                                    pass
                        grab = asyncio.create_task(grabber())
                        await run_fn(page)
                        self._set(i, status="completed", result="PASSED")
                    except Exception as e:  # noqa: BLE001
                        self._set(i, status="completed", result="FAILED", error=str(e))
                    finally:
                        stop.set()
                        if grab:
                            try: await grab
                            except Exception: pass  # noqa: BLE001,E722
                        await browser.close()
            finally:
                try:
                    client.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._set(i, live=False, phase="done",
                          shots=sorted(p.name for p in shot_dir.glob("step_*.png")))

        try:
            asyncio.run(drive())
        except Exception as e:  # noqa: BLE001
            self._set(i, status="completed", result="FAILED", error=str(e), live=False)
