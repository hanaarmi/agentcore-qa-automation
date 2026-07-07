"""QA Automation 데모 인프라 (CDK).

한 번의 `cdk deploy` 로 재현되는 리소스:
  1) AgentCore Runtime (agent/ 코드를 direct-code-deploy) — 변환+실행 에이전트
  2) 실행 역할(IAM) — Bedrock 모델 호출 + Browser Tool 세션 + Device Farm + logs/xray
  3) AWS Device Farm 프로젝트 + Device Pool (모바일 경로용)

주의:
- Browser Tool 은 시스템 브라우저(aws.browser.v1)를 쓰므로 생성할 리소스가 없다(권한만 부여).
- 로컬 대시보드(FastAPI)는 AWS 리소스가 아니라 배포 대상이 아님(README 참고).
- AgentCore Runtime 은 지원 리전에서만(예: us-west-2). Bedrock 모델 접근(Opus 4.8) 사전 활성 필요.
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

# deploy 자립형: 런타임 코드는 이 프로젝트 안의 agent/ (외부 참조 없음)
_AGENT_DIR = str((Path(__file__).resolve().parent.parent.parent / "agent"))
_RUNTIME_PY = "3.11"
# AgentCore Runtime 은 linux/arm64. 순수 파이썬 의존성이라 매니linux aarch64 휠로 설치 가능.
_PIP_PLATFORM = "manylinux2014_aarch64"


@jsii.implements(ILocalBundling)
class _LocalPipBundler:
    """Docker 없이 로컬에서 requirements 를 asset 출력 디렉토리에 설치.

    CDK from_code_asset 은 코드만 zip 하고 의존성을 설치하지 않는다 → OTEL 실행파일 누락.
    이 번들러가 code + deps 를 함께 담아 direct-code-deploy 번들을 완성한다.
    """

    def try_bundle(self, output_dir: str, *, image=None, **_) -> bool:  # noqa: ANN001
        # 1) 소스 복사
        subprocess.run(
            ["bash", "-c",
             f'cp {_AGENT_DIR}/*.py "{output_dir}/" && cp {_AGENT_DIR}/requirements.txt "{output_dir}/"'],
            check=True,
        )
        # 2) 의존성을 linux/arm64(py3.11) 휠로 output_dir 에 설치.
        #    --no-compile 필수: pip 은 install 후 host 인터프리터로 .pyc 를 재생성하는데
        #    (--python-version 무시), 그 host 버전(예: 3.12) .pyc 가 arm 런타임(3.11)과
        #    비호환이라 서비스가 "Python cache files incompatible" 로 거부한다.
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
        # 3) bin/ 콘솔 스크립트의 shebang 을 런타임에 존재하는 python 으로 교체.
        #    (로컬 pip 은 shebang 에 빌드 호스트 파이썬 절대경로를 박는데, 런타임엔 그 경로가
        #    없어 opentelemetry-instrument 실행이 깨진다. 토큿과 동일하게 env python3 로.)
        subprocess.run(
            ["bash", "-c",
             f'if [ -d "{output_dir}/bin" ]; then '
             f"  for f in \"{output_dir}\"/bin/*; do "
             f'    [ -f "$f" ] && sed -i.bak "1s|^#!.*python.*|#!/usr/bin/env python3|" "$f" && rm -f "$f.bak"; '
             f"  done; fi"],
            check=True,
        )
        # 4) 마지막에 .pyc/__pycache__ 제거 (host 버전 .pyc → arm 런타임 비호환).
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

        # --- 커스터마이즈 가능한 이름 (cdk deploy -c <key>=<value> 로 재정의) ---
        #   예) cdk deploy -c runtimeName=myQaAgent -c deviceFarmProject=my-qa -c devicePool=my-phones
        #   재정의 안 하면 아래 기본값 사용. (배포 계정마다 고칠 필요 있는 것들)
        runtime_name = self.node.try_get_context("runtimeName") or "qaConvertAgent"
        df_project_name = self.node.try_get_context("deviceFarmProject") or "qa-automation-demo"
        device_pool_name = self.node.try_get_context("devicePool") or "android-phones"

        # --- AgentCore Runtime: agent/ 코드 + 의존성을 direct-code-deploy ---
        artifact = agentcore.AgentRuntimeArtifact.from_code_asset(
            path="../agent",
            runtime=agentcore.AgentCoreRuntime.PYTHON_3_11,
            entrypoint=["opentelemetry-instrument", "runtime_app.py"],
            # 번들 로직 변경 시 재번들을 강제(입력 해시가 같아 캐시 재사용되는 것 방지).
            asset_hash="qa-agent-bundle-v3-nocompile-shebang",
            bundling=BundlingOptions(
                # Docker 이미지는 로컬 번들 실패 시 폴백. 우리는 로컬(pip)로 번들.
                image=DockerImage.from_registry("public.ecr.aws/docker/library/python:3.11"),
                local=_LocalPipBundler(),
                command=[],  # 로컬 번들러가 처리
            ),
        )

        runtime = agentcore.Runtime(
            self, "QaRuntime",
            runtime_name=runtime_name,
            agent_runtime_artifact=artifact,
            protocol_configuration=agentcore.ProtocolType.HTTP,
            environment_variables={"AWS_REGION": region},
        )

        # --- 실행 역할에 필요한 권한 추가 (L2 자동 역할에 얹음) ---
        # Bedrock 모델 호출 (Opus 4.8 인퍼런스 프로파일 포함)
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
        # AgentCore Browser Tool 세션 (시스템 브라우저 aws.browser.v1 리소스는 계정 'aws')
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
        # Device Farm (모바일 경로) — 데모 단순화를 위해 계정 범위. 운영 시 프로젝트 ARN 으로 축소.
        runtime.add_to_role_policy(iam.PolicyStatement(
            actions=["devicefarm:*"],
            resources=["*"],
        ))

        # --- AWS Device Farm 프로젝트 + Device Pool (모바일) ---
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

        # --- 대시보드가 쓸 값 출력 ---
        CfnOutput(self, "AgentRuntimeArn", value=runtime.agent_runtime_arn,
                  description="dashboard AGENTCORE_ARN 에 설정")
        CfnOutput(self, "Region", value=region)
        CfnOutput(self, "DeviceFarmProjectArn", value=df_project.attr_arn)
