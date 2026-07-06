#!/usr/bin/env python3
"""CDK 앱 진입점.

배포 리전은 지원 리전(예: us-west-2)이어야 한다(AgentCore Runtime + Browser Tool).
계정/리전은 CDK 기본 환경(CDK_DEFAULT_ACCOUNT/REGION 또는 --profile)에서 온다.
"""
import os
import aws_cdk as cdk

from stacks.qa_automation_stack import QaAutomationStack

app = cdk.App()

QaAutomationStack(
    app, "QaAutomationStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-west-2"),
    ),
    description="Agentic QA automation demo — AgentCore Runtime + Browser Tool + Device Farm",
)

app.synth()
