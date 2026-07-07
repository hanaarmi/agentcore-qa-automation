"""변환 에이전트 진입점.

scenario.json → 자연어 스텝 / Appium Python / Maestro YAML.

Strands Agent + Bedrock Claude Opus 4.8 를 "생성" 단계에만 사용한다.
실행 루프에는 LLM이 없다(설계 원칙: docs/ISSUES.md ISSUE-002 / D-005).

사용법:
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

# 모델은 배포 시 CDK context(-c modelId=...)로 Runtime 환경변수 QA_MODEL_ID 에 주입된다.
# 미설정 시 기본값(Opus 4.8 inference profile). 로컬 실행 시 export QA_MODEL_ID 로 변경 가능.
MODEL_ID = os.environ.get("QA_MODEL_ID", "us.anthropic.claude-opus-4-8")
REGION = os.environ.get("AWS_REGION", "us-west-2")

# target -> 기본 출력 확장자
EXT = {"appium": "py", "maestro": "yaml", "steps": "txt"}


def build_agent(target: str) -> Agent:
    system_prompt = SYSTEM_BY_TARGET[target]
    # 주의: Opus 4.8 은 temperature 파라미터를 더 이상 받지 않는다(deprecated) →
    # 전달하면 Bedrock ValidationException. 기본값에 맡긴다.
    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=REGION,
    )
    # callback_handler=None: 기본 핸들러가 스트리밍 토큰을 stdout 에 찍어 출력이
    # 중복되는 것을 막는다. 우리는 최종 텍스트만 한 번 출력한다.
    return Agent(model=model, system_prompt=system_prompt, callback_handler=None)


def convert_scenario(scenario: dict, target: str) -> str:
    """scenario 딕셔너리 → 대상 산출물 텍스트. CLI와 AgentCore 런타임이 공유하는 코어."""
    agent = build_agent(target)
    prompt = (
        "Convert the following scenario JSON.\n\n"
        f"```json\n{json.dumps(scenario, ensure_ascii=False, indent=2)}\n```"
    )
    result = agent(prompt)
    # Strands Agent 호출 결과에서 텍스트를 안전하게 추출.
    return _extract_text(result)


def convert(scenario_path: Path, target: str) -> str:
    """파일 경로 버전(CLI용). 코어는 convert_scenario 가 담당."""
    scenario = json.loads(scenario_path.read_text())
    return convert_scenario(scenario, target)


def _extract_text(result) -> str:
    """Strands AgentResult / message 구조에서 최종 텍스트만 뽑는다."""
    # AgentResult 는 str() 시 최종 응답 텍스트를 주도록 구현돼 있으나,
    # 버전차를 고려해 message 구조도 직접 처리.
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
