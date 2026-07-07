"""AgentCore Runtime entry point (conversion + web execution).

Two actions:
  - "convert"  : scenario/recording -> code/step generation (reuses convert_scenario)
  - "run_web"  : run a Playwright script inside this runtime against an AgentCore
                 Browser Tool session and return the result + per-step screenshots
                 (base64)

Core design: the dashboard invokes this runtime N times in parallel. Each invoke
opens its own Browser Tool session in its own microVM and runs independently, so
the orchestration load is spread across serverless (no load on the local box).

Deployment: the CDK in deploy/ pushes this code to the AgentCore Runtime
            (direct-code-deploy). See deploy/README.md. Manual deployment is also
            possible via the agentcore CLI.

payload:
  {"action":"convert", "scenario":{...}, "target":"appium|maestro|steps|playwright|..."}
  {"action":"run_web", "script":"<playwright async run(page) code>", "label":"var-1"}
"""
import base64
import os
import re

from bedrock_agentcore import BedrockAgentCoreApp

from convert import convert_scenario
from prompts import SYSTEM_BY_TARGET

app = BedrockAgentCoreApp()

_FENCE = re.compile(r"^```[a-zA-Z]*\n|\n```$")


def _clean(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        code = re.sub(r"^```[a-zA-Z]*\n", "", code)
        code = re.sub(r"\n```$", "", code)
    return code


def _run_web(script: str, label: str) -> dict:
    """Run a Playwright script in a Browser Tool session inside this runtime."""
    import asyncio
    from bedrock_agentcore.tools.browser_client import browser_session
    from playwright.async_api import async_playwright

    region = os.environ.get("AWS_REGION", "us-west-2")
    shot_dir = "/tmp/webshots"
    os.makedirs(shot_dir, exist_ok=True)
    os.environ["WEB_SHOT_DIR"] = shot_dir
    # Clear out any existing screenshots
    for f in os.listdir(shot_dir):
        if f.endswith(".png"):
            os.remove(os.path.join(shot_dir, f))

    # Load async def run(page) from the script
    ns: dict = {}
    exec(compile(_clean(script), "<script>", "exec"), ns)  # noqa: S102
    run_fn = ns.get("run")
    if run_fn is None:
        return {"label": label, "status": "failed", "error": "no async def run(page)"}

    async def drive():
        result = {"label": label, "status": "running", "error": None}
        with browser_session(region) as client:
            ws_url, headers = client.generate_ws_headers()
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(ws_url, headers=headers)
                try:
                    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await run_fn(page)
                    result["status"] = "passed"
                except Exception as e:  # noqa: BLE001
                    result["status"] = "failed"
                    result["error"] = str(e)
                finally:
                    await browser.close()
        return result

    result = asyncio.run(drive())

    # Return the screenshots as base64 (in step order).
    shots = []
    for name in sorted(os.listdir(shot_dir)):
        if name.endswith(".png"):
            with open(os.path.join(shot_dir, name), "rb") as fh:
                shots.append({"name": name, "b64": base64.b64encode(fh.read()).decode()})
    result["shots"] = shots
    return result


def _scenario_run(brief: str, base_code: str, label: str) -> dict:
    """Brief (one sentence) -> generate a Playwright script with the LLM inside the runtime -> run via Browser Tool.

    This way script generation (the LLM) is also distributed across each runtime
    (not run locally).
    """
    from convert import convert_scenario
    # Feed brief + base_code to generate a script with the playwright_from_brief target.
    gen_input = {"brief": brief, "base_script": base_code}
    script = convert_scenario(gen_input, "playwright_from_brief")
    out = _run_web(script, label)
    out["brief"] = brief
    return out


@app.entrypoint
def handler(payload, context=None):
    action = payload.get("action", "convert")

    if action == "run_web":
        script = payload.get("script")
        if not script:
            return {"error": "run_web requires 'script'"}
        return _run_web(script, payload.get("label", "web"))

    if action == "scenario_run":
        brief = payload.get("brief")
        base_code = payload.get("base_script", "")
        if not brief:
            return {"error": "scenario_run requires 'brief'"}
        return _scenario_run(brief, base_code, payload.get("label", "web"))

    # Default: conversion
    scenario = payload.get("scenario")
    target = payload.get("target", "appium")
    if scenario is None:
        return {"error": "payload must include 'scenario'"}
    if target not in SYSTEM_BY_TARGET:
        return {"error": f"target must be one of {sorted(SYSTEM_BY_TARGET)}", "got": target}
    return {"target": target, "output": convert_scenario(scenario, target)}


if __name__ == "__main__":
    app.run()
