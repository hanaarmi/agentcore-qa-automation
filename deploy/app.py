#!/usr/bin/env python3
"""CDK app entry point.

The deployment region must be a supported region (e.g. us-west-2) for
AgentCore Runtime + Browser Tool. Account/region are taken from the CDK
default environment (CDK_DEFAULT_ACCOUNT/REGION or --profile).
"""
import os
import aws_cdk as cdk

from stacks.qa_automation_stack import QaAutomationStack

app = cdk.App()

# The stack name can be overridden via context (-c stackName=...) to deploy without collisions across accounts.
stack_name = app.node.try_get_context("stackName") or "QaAutomationStack"

QaAutomationStack(
    app, stack_name,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-west-2"),
    ),
    description="Agentic QA automation demo — AgentCore Runtime + Browser Tool + Device Farm",
)

app.synth()
