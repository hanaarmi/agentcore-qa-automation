"""Conversion agent entry point.

scenario.json -> natural-language steps / Appium Python / Maestro YAML.

The Strands Agent + Bedrock Claude Opus 4.8 is used only for the "generation"
stage. The execution loop has no LLM (design principle: generation uses the LLM,
execution uses deterministic code).

Usage:
    python agent/convert.py <scenario.json> --target {appium|maestro|steps} [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from strands import Agent
from strands.models import BedrockModel

from prompts import SYSTEM_BY_TARGET

# The model is injected into the Runtime env var QA_MODEL_ID via CDK context
# (-c modelId=...) at deploy time. If unset, it defaults to the Opus 4.8 inference
# profile. For local runs, override with export QA_MODEL_ID.
MODEL_ID = os.environ.get("QA_MODEL_ID", "us.anthropic.claude-opus-4-8")
REGION = os.environ.get("AWS_REGION", "us-west-2")

# target -> default output file extension
EXT = {"appium": "py", "maestro": "yaml", "steps": "txt"}


def build_agent(target: str) -> Agent:
    system_prompt = SYSTEM_BY_TARGET[target]
    # Note: Opus 4.8 no longer accepts the temperature parameter (deprecated);
    # passing it raises a Bedrock ValidationException. Rely on the default.
    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=REGION,
    )
    # callback_handler=None: prevents the default handler from printing streaming
    # tokens to stdout, which would duplicate output. We emit the final text once.
    return Agent(model=model, system_prompt=system_prompt, callback_handler=None)


def convert_scenario(scenario: dict, target: str) -> str:
    """Scenario dict -> target artifact text. Core shared by the CLI and the AgentCore runtime."""
    agent = build_agent(target)
    prompt = (
        "Convert the following scenario JSON.\n\n"
        f"```json\n{json.dumps(scenario, ensure_ascii=False, indent=2)}\n```"
    )
    result = agent(prompt)
    # Safely extract the text from the Strands Agent call result.
    return _extract_text(result)


def convert(scenario_path: Path, target: str) -> str:
    """File-path variant (for the CLI). The core work is handled by convert_scenario."""
    scenario = json.loads(scenario_path.read_text())
    return convert_scenario(scenario, target)


def _extract_text(result) -> str:
    """Extract only the final text from a Strands AgentResult / message structure."""
    # AgentResult is implemented to return the final response text via str(), but
    # we also handle the message structure directly to tolerate version differences.
    msg = getattr(result, "message", None)
    if isinstance(msg, dict):
        parts = msg.get("content", [])
        texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
        joined = "".join(texts).strip()
        if joined:
            return joined
    return str(result).strip()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="scenario.json -> test artifact")
    ap.add_argument("scenario", type=Path, help="path to scenario.json")
    ap.add_argument(
        "--target",
        choices=sorted(SYSTEM_BY_TARGET),
        default="appium",
        help="output kind (default: appium)",
    )
    ap.add_argument("--out", type=Path, default=None, help="write to file instead of stdout")
    args = ap.parse_args(argv)

    if not args.scenario.is_file():
        print(f"error: scenario not found: {args.scenario}", file=sys.stderr)
        return 2

    output = convert(args.scenario, args.target)

    if args.out:
        args.out.write_text(output + "\n")
        print(f"wrote {args.out} ({len(output)} chars)", file=sys.stderr)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
