"""Agent invocation adapter.

Lets the dashboard backend call the conversion agent through the **same function
signature** whether the backend is local (convert.py) or serverless (AgentCore).
Switched via a single environment variable:

    AGENT_BACKEND=local      # call convert_scenario directly in-process (default)
    AGENT_BACKEND=agentcore  # invoke the deployed AgentCore Runtime

When using AgentCore, the ARN (AgentRuntimeArn from the CDK deploy outputs):
    AGENTCORE_ARN=arn:aws:bedrock-agentcore:us-west-2:<account>:runtime/<runtimeName>-<id>
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add agent/ to the import path (reuse convert_scenario).
_AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

# Region where the AgentCore runtime is deployed. Note: the ambient AWS_REGION may be
# a different value (e.g. us-east-1), so do not rely on it. Accept a dedicated override
# only via AGENTCORE_REGION.
REGION = os.environ.get("AGENTCORE_REGION", "us-west-2")


def convert(scenario: dict, target: str) -> str:
    """scenario + target -> output text. Backend selected via env var."""
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
        raise RuntimeError("AGENT_BACKEND=agentcore but AGENTCORE_ARN is not set.")

    client = boto3.client("bedrock-agentcore", region_name=REGION)
    payload = json.dumps({"scenario": scenario, "target": target}).encode("utf-8")
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        qualifier=os.environ.get("AGENTCORE_QUALIFIER", "DEFAULT"),
        payload=payload,
        contentType="application/json",
        accept="application/json",
    )
    # The response body is a StreamingBody under the "response" key. Read it and parse
    # the {"target", "output"} payload.
    body = resp["response"].read()
    data = json.loads(body)
    if isinstance(data, dict) and "output" in data:
        return data["output"]
    return body.decode("utf-8")
