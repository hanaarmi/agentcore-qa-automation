# QA Automation on AWS — 배포 (CDK, 자립형)

`cdk deploy` 한 번으로 AWS 인프라를 재현합니다. 이 폴더는 **자립형**입니다 — 런타임 코드(`agent/`)를
안에 포함하므로 이 디렉토리만 clone 해도 배포됩니다.

## 구성

```
.
├── app.py, cdk.json, requirements.txt   # CDK 앱
├── stacks/qa_automation_stack.py        # 스택 정의
├── agent/                               # ★ AgentCore Runtime 코드 (내장, direct-code-deploy)
│   ├── runtime_app.py                   #   엔트리포인트 (convert + web 실행)
│   ├── convert.py / prompts.py          #   시나리오→스크립트 변환 로직
│   └── requirements.txt                 #   런타임 의존성 (번들 시 설치)
└── README.md
```

## 무엇이 배포되나

| 리소스 | 설명 |
|---|---|
| `AWS::BedrockAgentCore::Runtime` | 변환+실행 에이전트 (`agent/` 코드를 direct-code-deploy) |
| IAM 실행 역할 | Bedrock 모델 호출 + Browser Tool 세션 + Device Farm + logs/xray |
| `AWS::DeviceFarm::Project` + `DevicePool` | 모바일(Appium) 경로용 |

> Browser Tool 은 시스템 브라우저(`aws.browser.v1`)를 쓰므로 생성할 리소스가 없습니다(권한만 부여).
> 대시보드(로컬 FastAPI)는 AWS 리소스가 아니라 이 배포에 포함되지 않습니다(맨 아래 참고).

## AWS 계정은 어떤 걸 쓰고 어떻게 세팅하나 (간단 가이드)

- **어떤 계정?** 본인/조직의 **일반 AWS 계정** 하나면 됩니다. 특별한 계정 종류 불필요.
  이 스택은 그 계정 안에 리소스를 만들고, `cdk destroy` 로 지웁니다.
- **리전 주의**: **`us-west-2`(오레곤)** 을 쓰세요. AgentCore Runtime 지원 리전이고,
  **AWS Device Farm 은 사실상 us-west-2 전용**(서울 등 다른 리전엔 엔드포인트가 없음)입니다.
- **자격증명 세팅** (둘 중 하나):
  ```bash
  aws configure                 # Access Key/Secret + region=us-west-2 입력
  #   또는 SSO:
  aws configure sso             # 조직 SSO 사용 시
  ```
  확인: `aws sts get-caller-identity` (계정ID가 나오면 OK).
- **셸 리전 함정**: `AWS_REGION` 환경변수가 다른 값(예: us-east-1)이면 CDK 가 그리로 배포합니다.
  배포 시 `AWS_REGION=us-west-2` 를 함께 주는 걸 권장(아래 명령들처럼).
- **필요 권한**: 배포 계정에 CloudFormation / IAM(역할 생성) / Bedrock AgentCore / Device Farm /
  ECR·S3(에셋) 권한이 있어야 합니다. 관리자 계정이면 그대로 됩니다.

## 사전 준비

1. **AWS 계정 + 자격증명** (위 가이드). 리전 **us-west-2**.
2. **Bedrock 모델 접근 활성화**: 콘솔 → Bedrock → Model access 에서 **Claude Opus 4.8** 활성.
   (이게 없으면 런타임이 모델 호출에서 AccessDenied.)
3. **Node.js 22+** (CDK CLI 용), **Python 3.11+**, **Docker 불필요**(direct-code-deploy).
4. CDK 부트스트랩(계정/리전당 1회):
   ```bash
   python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
   AWS_REGION=us-west-2 CDK_DEFAULT_REGION=us-west-2 \
     npx aws-cdk@2 bootstrap --app "./.venv/bin/python app.py"
   ```

## 배포

```bash
cd deploy
AWS_REGION=us-west-2 CDK_DEFAULT_REGION=us-west-2 npx aws-cdk@2 deploy --app "./.venv/bin/python app.py"
```

배포가 끝나면 출력(Outputs)에서 다음을 확인:
- `AgentRuntimeArn` → 대시보드 환경변수 `AGENTCORE_ARN`
- `Region`, `DeviceFarmProjectArn`

## 이름 커스터마이즈 (선택)

리소스 이름은 CDK context(`-c key=value`)로 재정의할 수 있습니다. 안 주면 기본값을 씁니다.

| context 키 | 기본값 | 대상 |
|---|---|---|
| `runtimeName` | `qaConvertAgent` | AgentCore Runtime 이름 (`[a-zA-Z][a-zA-Z0-9_]{0,47}`) |
| `deviceFarmProject` | `qa-automation-demo` | Device Farm 프로젝트 이름 |
| `devicePool` | `android-phones` | Device Pool 이름 |
| `modelId` | `us.anthropic.claude-opus-4-8` | 생성/변형에 쓰는 Bedrock 모델 (Runtime 에 `QA_MODEL_ID` env 로 주입) |

예시:
```bash
AWS_REGION=us-west-2 CDK_DEFAULT_REGION=us-west-2 \
  npx aws-cdk@2 deploy --app "./.venv/bin/python app.py" \
  -c runtimeName=myQaAgent \
  -c deviceFarmProject=my-qa-project \
  -c devicePool=my-android-phones \
  -c modelId=us.anthropic.claude-sonnet-4-5-20250929-v1:0
```

> 계정마다 이름 충돌을 피하려면 `runtimeName` 을 고유하게 주는 것을 권장합니다.
> `modelId` 는 **본인 계정에서 Model access 가 활성화된** inference profile/모델 ID 여야 합니다
> (미설정 시 Opus 4.8). 사전 준비 2번의 모델 활성화 대상과 일치시키세요.

## 배포 검증 (invoke)

배포된 런타임이 도는지 바로 확인:
```bash
AWS_REGION=us-west-2 ./.venv/bin/python - <<'PY'
import boto3, json
arn = "<배포 출력의 AgentRuntimeArn>"
c = boto3.client("bedrock-agentcore", region_name="us-west-2")
r = c.invoke_agent_runtime(
    agentRuntimeArn=arn, qualifier="DEFAULT",
    payload=json.dumps({"action":"convert","scenario":{"scenarioName":"t","actions":[]},"target":"steps"}).encode(),
    contentType="application/json", accept="application/json",
    runtimeSessionId="verify-"+"0"*30)
print(json.loads(r["response"].read()))
PY
```

## 대시보드 (선택 · 별도)

이 배포 리포는 **인프라(런타임)** 만 담습니다. 로컬 FastAPI 대시보드는 전체 데모 리포에 있으며,
배포 출력의 `AgentRuntimeArn` 을 환경변수로 넣어 실행합니다:
```bash
AGENT_BACKEND=agentcore AGENTCORE_ARN=<AgentRuntimeArn> AGENTCORE_REGION=us-west-2 \
  python -m uvicorn dashboard.server:app --port 8000   # 대시보드 리포에서
```
GitHub Pages 같은 정적 호스팅으로는 못 올립니다(백엔드가 AWS 자격증명으로 런타임을 호출).

## 검증만 (배포 안 함)

```bash
cd deploy
AWS_REGION=us-west-2 CDK_DEFAULT_REGION=us-west-2 npx aws-cdk@2 synth --app "./.venv/bin/python app.py"
```

## 삭제 (teardown)

```bash
cd deploy
AWS_REGION=us-west-2 CDK_DEFAULT_REGION=us-west-2 npx aws-cdk@2 destroy --app "./.venv/bin/python app.py"
```

## 비용 주의

- AgentCore Runtime / Browser Tool: 사용(세션·분) 기반 과금. Device Farm: 실행 분 기반($0.17/분,
  최초 1,000분 무료). 병렬 N개 실행은 그만큼 곱해집니다. 데모 후 `destroy` 권장.

## 참고

- AgentCore Runtime 은 CFN 리소스 `AWS::BedrockAgentCore::Runtime`, CDK L2
  `aws_cdk.aws_bedrockagentcore.Runtime` (aws-cdk-lib >= 2.261.0) 로 배포됩니다.
- 스택 정의: [stacks/qa_automation_stack.py](stacks/qa_automation_stack.py)
