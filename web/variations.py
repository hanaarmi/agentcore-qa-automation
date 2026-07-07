"""Variation generator.

Feeds the base Playwright script and screenshot from an initial run to the LLM
(Opus, multimodal) to generate N meaningful variations. Each variation keeps the
same page/selector strategy but changes the data and flow (different input values,
item counts, edge cases, etc.).

Usage:
    from web.variations import make_variations
    scripts = make_variations(base_code, screenshot_bytes, n=20)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_AGENT = str(Path(__file__).resolve().parent.parent / "agent")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

from strands import Agent  # noqa: E402
from strands.models import BedrockModel  # noqa: E402
from prompts import PLAYWRIGHT_VARIATION_SYSTEM, SCENARIO_BRAINSTORM_SYSTEM  # noqa: E402
from convert import _extract_text  # noqa: E402

# The model can be overridden via env (QA_MODEL_ID); defaults to Opus 4.8 if unset
# (injected via CDK context at deploy time).
MODEL_ID = os.environ.get("QA_MODEL_ID", "us.anthropic.claude-opus-4-8")
REGION = os.environ.get("AWS_REGION", "us-west-2")

# Minimal fallback briefs used if brainstorming fails.
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
    """Have the LLM propose N distinct test scenarios from the screenshot (one call, local).

    Returns: [{"title": short title, "desc": description, "brief": instruction
    for code generation}, ...]
    Script generation/execution is performed by each runtime given the brief (not
    done locally).
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
                out.append({"title": str(x.get("title", "")).strip() or "Scenario",
                            "desc": str(x.get("desc", "")).strip(),
                            "brief": str(x["brief"]).strip()})
    except Exception:  # noqa: BLE001
        out = []
    # Pad to the requested count (fallback).
    while len(out) < n:
        i = len(out)
        out.append({"title": f"Scenario {i + 1}", "desc": _FALLBACK[i % len(_FALLBACK)],
                    "brief": _FALLBACK[i % len(_FALLBACK)]})
    return out[:n]
