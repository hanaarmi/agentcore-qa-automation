"""Variation 생성기.

한 번 돌려서 나온 base Playwright 스크립트 + 스크린샷을 LLM(Opus, 멀티모달)에 던져
의미 있는 변형 N개를 생성한다. 각 변형은 같은 페이지/셀렉터 전략을 유지하되 데이터·흐름을
바꾼다(다른 입력값, 항목 수, 엣지케이스 등).

사용:
    from web.variations import make_variations
    scripts = make_variations(base_code, screenshot_bytes, n=20)
"""
from __future__ import annotations

import sys
from pathlib import Path

_AGENT = str(Path(__file__).resolve().parent.parent / "agent")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

from strands import Agent  # noqa: E402
from strands.models import BedrockModel  # noqa: E402
from prompts import PLAYWRIGHT_VARIATION_SYSTEM, SCENARIO_BRAINSTORM_SYSTEM  # noqa: E402
from convert import _extract_text  # noqa: E402

MODEL_ID = "us.anthropic.claude-opus-4-8"
REGION = "us-west-2"

# 브레인스토밍 실패 시 최소한의 폴백 브리프.
_FALLBACK = [
    "Add two todos with ordinary text, then complete the first.",
    "Add a todo with a very long (60+ char) title, then complete it.",
    "Add three todos, then complete the last one.",
    "Add a todo with special characters and emoji, then complete it.",
    "Add a todo, complete it, then re-open (uncheck) it.",
]


def _agent(system: str = PLAYWRIGHT_VARIATION_SYSTEM) -> Agent:
    model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
    return Agent(model=model, system_prompt=system, callback_handler=None)


def brainstorm_scenarios(base_code: str, screenshot: bytes | None, n: int) -> list[dict]:
    """스크린샷을 보고 서로 다른 테스트 시나리오 N개를 LLM이 예상(1회, 로컬).

    반환: [{"title": 한국어 제목, "desc": 한국어 설명, "brief": 영어 코드생성용 지시}, ...]
    스크립트 생성/실행은 각 runtime 이 brief 를 받아 수행한다(로컬에서 안 함).
    """
    import json
    agent = _agent(SCENARIO_BRAINSTORM_SYSTEM)
    content: list = [{"text":
        f"Base test:\n```python\n{base_code}\n```\n\n"
        f"Brainstorm exactly {n} DISTINCT test scenarios for this app as the specified JSON array."}]
    if screenshot:
        content.append({"image": {"format": "png", "source": {"bytes": screenshot}}})
    raw = _extract_text(agent([{"role": "user", "content": content}]))
    try:
        s = raw[raw.index("["): raw.rindex("]") + 1]
        items = json.loads(s)
        out = []
        for x in items:
            if isinstance(x, dict) and x.get("brief"):
                out.append({"title": str(x.get("title", "")).strip() or "시나리오",
                            "desc": str(x.get("desc", "")).strip(),
                            "brief": str(x["brief"]).strip()})
    except Exception:  # noqa: BLE001
        out = []
    # 개수 보정(폴백).
    while len(out) < n:
        i = len(out)
        out.append({"title": f"기본 시나리오 {i + 1}", "desc": _FALLBACK[i % len(_FALLBACK)],
                    "brief": _FALLBACK[i % len(_FALLBACK)]})
    return out[:n]
