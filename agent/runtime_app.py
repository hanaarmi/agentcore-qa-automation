"""AgentCore Runtime 엔트리포인트 (변환 + 웹 실행).

두 가지 action:
  - "convert"  : scenario/recording → 코드/스텝 생성 (convert_scenario 재사용)
  - "run_web"  : Playwright 스크립트를 이 런타임 안에서 AgentCore Browser Tool 세션으로
                 실행하고 결과 + 스텝별 스크린샷(base64)을 반환

핵심 설계: 대시보드는 이 런타임을 **N번 병렬 invoke** 한다. 각 invoke 가 자기 microVM 에서
자기 Browser Tool 세션을 열어 독립 실행 → 오케스트레이션 부하가 서버리스로 분산된다
(로컬 박스에 부하 없음).

배포: deploy/ 의 CDK 로 이 코드를 AgentCore Runtime(direct-code-deploy)에 올린다.
      (deploy/README.md 참고. 수동 배포는 agentcore CLI 로도 가능.)

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
    """Playwright 스크립트를 이 런타임 안의 Browser Tool 세션에서 실행."""
    import asyncio
    from bedrock_agentcore.tools.browser_client import browser_session
    from playwright.async_api import async_playwright

    region = os.environ.get("AWS_REGION", "us-west-2")
    shot_dir = "/tmp/webshots"
    os.makedirs(shot_dir, exist_ok=True)
    os.environ["WEB_SHOT_DIR"] = shot_dir
    # 기존 스크린샷 정리
    for f in os.listdir(shot_dir):
        if f.endswith(".png"):
            os.remove(os.path.join(shot_dir, f))

    # 스크립트에서 async def run(page) 로드
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

    # 스크린샷을 base64 로 반환(스텝 순서).
    shots = []
    for name in sorted(os.listdir(shot_dir)):
        if name.endswith(".png"):
            with open(os.path.join(shot_dir, name), "rb") as fh:
                shots.append({"name": name, "b64": base64.b64encode(fh.read()).decode()})
    result["shots"] = shots
    return result


def _scenario_run(brief: str, base_code: str, label: str) -> dict:
    """브리프(한 문장) → runtime 안에서 LLM 으로 Playwright 스크립트 생성 → Browser Tool 실행.

    이렇게 하면 스크립트 생성(LLM)도 각 runtime 에 분산된다(로컬에서 안 돌림).
    """
    from convert import convert_scenario
    # brief + base_code 를 넣어 playwright_from_brief 타깃으로 스크립트 생성.
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

    # 기본: 변환
    scenario = payload.get("scenario")
    target = payload.get("target", "appium")
    if scenario is None:
        return {"error": "payload must include 'scenario'"}
    if target not in SYSTEM_BY_TARGET:
        return {"error": f"target must be one of {sorted(SYSTEM_BY_TARGET)}", "got": target}
    return {"target": target, "output": convert_scenario(scenario, target)}


if __name__ == "__main__":
    app.run()
