"""에이전트 호출 어댑터.

대시보드 백엔드가 변환 에이전트를 부를 때, 로컬(convert.py)이든 서버리스(AgentCore)든
**같은 함수 시그니처**로 부르게 한다. 환경변수 하나로 전환:

    AGENT_BACKEND=local      # 로컬 convert_scenario 직접 호출 (기본)
    AGENT_BACKEND=agentcore  # 배포된 AgentCore Runtime 을 invoke

AgentCore 사용 시 ARN:
    AGENTCORE_ARN=arn:aws:bedrock-agentcore:us-west-2:...:runtime/wbkqaconvert-...
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# agent/ 를 import 경로에 추가 (convert_scenario 재사용).
_AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

# AgentCore 런타임이 배포된 리전. 주의: 앰비언트 AWS_REGION 이 다른 값(예: us-east-1)일 수
# 있으므로 그것에 의존하지 않는다. 전용 오버라이드는 AGENTCORE_REGION 로만 받는다.
REGION = os.environ.get("AGENTCORE_REGION", "us-west-2")


def convert(scenario: dict, target: str) -> str:
    """scenario + target → 산출물 텍스트. 백엔드는 env로 선택."""
    backend = os.environ.get("AGENT_BACKEND", "local")
    if backend == "agentcore":
        return _convert_via_agentcore(scenario, target)
    return _convert_local(scenario, target)


def _convert_local(scenario: dict, target: str) -> str:
    from convert import convert_scenario  # agent/convert.py
    return convert_scenario(scenario, target)


def _convert_via_agentcore(scenario: dict, target: str) -> str:
    import boto3

    arn = os.environ.get("AGENTCORE_ARN")
    if not arn:
        raise RuntimeError("AGENT_BACKEND=agentcore 인데 AGENTCORE_ARN 이 없습니다.")

    client = boto3.client("bedrock-agentcore", region_name=REGION)
    payload = json.dumps({"scenario": scenario, "target": target}).encode("utf-8")
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        qualifier=os.environ.get("AGENTCORE_QUALIFIER", "DEFAULT"),
        payload=payload,
        contentType="application/json",
        accept="application/json",
    )
    # 응답 본문은 "response" 키의 StreamingBody. 읽어서 {"target","output"} 파싱.
    body = resp["response"].read()
    data = json.loads(body)
    if isinstance(data, dict) and "output" in data:
        return data["output"]
    return body.decode("utf-8")
