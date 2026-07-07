"""QA Automation demo infrastructure (CDK).

Resources provisioned by a single `cdk deploy`:
  1) AgentCore Runtime (direct-code-deploy of the agent/ code) — the conversion + execution agent
  2) Execution role (IAM) — Bedrock model invocation + Browser Tool sessions + Device Farm + logs/xray
  3) AWS Device Farm project + Device Pool (for the mobile path)

Notes:
- The Browser Tool uses the system browser (aws.browser.v1), so there is no resource to create (permissions only).
- The local dashboard (FastAPI) is not an AWS resource and is not part of this deployment (see README).
- AgentCore Runtime is only available in supported regions (e.g. us-west-2). Bedrock model access (Opus 4.8) must be enabled beforehand.
"""
import subprocess
from pathlib import Path

import jsii
from aws_cdk import (
    Stack,
    CfnOutput,
    BundlingOptions,
    DockerImage,
    ILocalBundling,
    aws_iam as iam,
    aws_devicefarm as devicefarm,
    aws_bedrockagentcore as agentcore,
)
from constructs import Construct

# Self-contained deploy: the runtime code lives in this project's agent/ directory (no external references).
_AGENT_DIR = str((Path(__file__).resolve().parent.parent.parent / "agent"))
_RUNTIME_PY = "3.11"
# AgentCore Runtime is linux/arm64. Dependencies are pure Python, so they can be installed from manylinux aarch64 wheels.
_PIP_PLATFORM = "manylinux2014_aarch64"


@jsii.implements(ILocalBundling)
class _LocalPipBundler:
    """Install requirements into the asset output directory locally, without Docker.

    CDK from_code_asset only zips the code and does not install dependencies,
    which leaves the OTEL executable missing. This bundler packages the code
    together with its dependencies to complete a direct-code-deploy bundle.
    """

    def try_bundle(self, output_dir: str, *, image=None, **_) -> bool:  # noqa: ANN001
        # 1) Copy the source files.
        subprocess.run(
            ["bash", "-c",
             f'cp {_AGENT_DIR}/*.py "{output_dir}/" && cp {_AGENT_DIR}/requirements.txt "{output_dir}/"'],
            check=True,
        )
        # 2) Install dependencies into output_dir as linux/arm64 (py3.11) wheels.
        #    --no-compile is required: after installing, pip regenerates .pyc files
        #    using the host interpreter (ignoring --python-version), and those host
        #    version (e.g. 3.12) .pyc files are incompatible with the arm runtime
        #    (3.11), causing the service to reject them with "Python cache files incompatible".
        subprocess.run(
            ["python3", "-m", "pip", "install",
             "-r", f"{_AGENT_DIR}/requirements.txt",
             "--target", output_dir,
             "--platform", _PIP_PLATFORM,
             "--python-version", _RUNTIME_PY,
             "--implementation", "cp",
             "--abi", "cp311",
             "--only-binary=:all:",
             "--no-compile",
             "--upgrade"],
            check=True,
        )
        # 3) Rewrite the shebang of the bin/ console scripts to a python that
        #    exists on the runtime. Local pip embeds the build host's absolute
        #    python path in the shebang, which does not exist on the runtime and
        #    breaks opentelemetry-instrument. Replace it with env python3.
        subprocess.run(
            ["bash", "-c",
             f'if [ -d "{output_dir}/bin" ]; then '
             f"  for f in \"{output_dir}\"/bin/*; do "
             f'    [ -f "$f" ] && sed -i.bak "1s|^#!.*python.*|#!/usr/bin/env python3|" "$f" && rm -f "$f.bak"; '
             f"  done; fi"],
            check=True,
        )
        # 4) Finally, remove .pyc/__pycache__ (host version .pyc is incompatible with the arm runtime).
        subprocess.run(
            ["bash", "-c",
             f'find "{output_dir}" -type d -name __pycache__ -prune -exec rm -rf {{}} + ; '
             f'find "{output_dir}" -type f -name "*.pyc" -delete ; '
             f'find "{output_dir}" -type f -name "*.pyo" -delete'],
            check=True,
        )
        return True


class QaAutomationStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        region = self.region
        account = self.account

        # --- Customizable names (override with cdk deploy -c <key>=<value>) ---
        #   e.g. cdk deploy -c runtimeName=myQaAgent -c deviceFarmProject=my-qa -c devicePool=my-phones
        #   If not overridden, the defaults below are used. (These typically need to change per deployment account.)
        runtime_name = self.node.try_get_context("runtimeName") or "qaConvertAgent"
        df_project_name = self.node.try_get_context("deviceFarmProject") or "qa-automation-demo"
        device_pool_name = self.node.try_get_context("devicePool") or "android-phones"
        # Bedrock model used for generation/conversion. The runtime code reads it from the QA_MODEL_ID env var.
        # To switch to a model enabled in your account (Model access), override with -c modelId=...
        model_id = self.node.try_get_context("modelId") or "us.anthropic.claude-opus-4-8"

        # --- AgentCore Runtime: direct-code-deploy of the agent/ code + dependencies ---
        artifact = agentcore.AgentRuntimeArtifact.from_code_asset(
            path="../agent",
            runtime=agentcore.AgentCoreRuntime.PYTHON_3_11,
            entrypoint=["opentelemetry-instrument", "runtime_app.py"],
            # Force a re-bundle when the bundling logic changes (prevents cache reuse when the input hash is unchanged).
            asset_hash="qa-agent-bundle-v3-nocompile-shebang",
            bundling=BundlingOptions(
                # The Docker image is a fallback if local bundling fails; bundling is done locally via pip.
                image=DockerImage.from_registry("public.ecr.aws/docker/library/python:3.11"),
                local=_LocalPipBundler(),
                command=[],  # handled by the local bundler
            ),
        )

        runtime = agentcore.Runtime(
            self, "QaRuntime",
            runtime_name=runtime_name,
            agent_runtime_artifact=artifact,
            protocol_configuration=agentcore.ProtocolType.HTTP,
            environment_variables={"AWS_REGION": region, "QA_MODEL_ID": model_id},
        )

        # --- Add the permissions required by the execution role (attached to the L2 auto-generated role) ---
        # Bedrock model invocation (including the Opus 4.8 inference profile)
        runtime.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ],
            resources=[
                "arn:aws:bedrock:*::foundation-model/*",
                f"arn:aws:bedrock:*:{account}:inference-profile/*",
                "arn:aws:bedrock:*:aws:inference-profile/*",
            ],
        ))
        # AgentCore Browser Tool sessions (the system browser aws.browser.v1 resource is owned by account 'aws')
        runtime.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock-agentcore:StartBrowserSession",
                "bedrock-agentcore:StopBrowserSession",
                "bedrock-agentcore:GetBrowserSession",
                "bedrock-agentcore:ListBrowserSessions",
                "bedrock-agentcore:ConnectBrowserAutomationStream",
                "bedrock-agentcore:UpdateBrowserStream",
                "bedrock-agentcore:ConnectBrowserLiveViewStream",
            ],
            resources=[
                f"arn:aws:bedrock-agentcore:{region}:aws:browser/*",
                f"arn:aws:bedrock-agentcore:{region}:{account}:browser/*",
                f"arn:aws:bedrock-agentcore:{region}:{account}:browser-session/*",
            ],
        ))
        # Device Farm (mobile path) — account-scoped for demo simplicity. In production, narrow this to the project ARN.
        runtime.add_to_role_policy(iam.PolicyStatement(
            actions=["devicefarm:*"],
            resources=["*"],
        ))

        # --- AWS Device Farm project + Device Pool (mobile) ---
        df_project = devicefarm.CfnProject(
            self, "DeviceFarmProject",
            name=df_project_name,
        )
        devicefarm.CfnDevicePool(
            self, "DeviceFarmPool",
            name=device_pool_name,
            project_arn=df_project.attr_arn,
            description="Android phones, highly available (demo)",
            max_devices=1,
            rules=[
                devicefarm.CfnDevicePool.RuleProperty(
                    attribute="PLATFORM", operator="EQUALS", value='"ANDROID"'),
                devicefarm.CfnDevicePool.RuleProperty(
                    attribute="FORM_FACTOR", operator="EQUALS", value='"PHONE"'),
                devicefarm.CfnDevicePool.RuleProperty(
                    attribute="AVAILABILITY", operator="EQUALS", value='"HIGHLY_AVAILABLE"'),
            ],
        )

        # --- Output values consumed by the dashboard ---
        CfnOutput(self, "AgentRuntimeArn", value=runtime.agent_runtime_arn,
                  description="set as the dashboard AGENTCORE_ARN")
        CfnOutput(self, "Region", value=region)
        CfnOutput(self, "ModelId", value=model_id, description="Bedrock model used by the Runtime")
        CfnOutput(self, "DeviceFarmProjectArn", value=df_project.attr_arn)
