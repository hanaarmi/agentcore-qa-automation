"""Device Farm 설정 리졸버 (대시보드/실행 스크립트 공용).

config.json 은 `devicefarm_setup.py` 가 만드는 산출물이지 리포에 박힌 값이 아니다.
CDK 배포는 프로젝트/풀을 만들지만 이 파일은 안 만든다 → 대시보드가 그걸 못 찾는 갭이
생긴다. 이 리졸버가 그 갭을 메운다. 우선순위:

  1) infra/config.json 이 있으면 그대로 사용.
  2) 없으면 env(DEVICEFARM_PROJECT_ARN / DEVICEFARM_POOL_ARN)로 구성.
  3) 그것도 없으면 **이름으로 Device Farm 을 조회**한다. 이름은 env
     (DEVICEFARM_PROJECT / DEVICEFARM_POOL) 또는 CDK 기본값(qa-automation-demo /
     android-phones). CDK 배포 시 이름을 -c 로 바꿨다면 같은 값을 env 로 주면 된다.

조회로 찾으면 config.json 에 캐시해 다음부터 API 호출을 아낀다. 즉, 배포만 했으면
수동으로 devicefarm_setup.py 를 돌리지 않아도 모바일 탭이 열린다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")

# CDK 스택 기본값과 일치(deploy/stacks/qa_automation_stack.py). -c 로 바꿨으면 env 로 알려준다.
DEFAULT_PROJECT_NAME = "qa-automation-demo"
DEFAULT_POOL_NAME = "android-phones"


def _region() -> str:
    return (
        os.environ.get("DEVICEFARM_REGION")
        or os.environ.get("AGENTCORE_REGION")
        or "us-west-2"
    )


def _find_project(client, name: str) -> str | None:
    for page in client.get_paginator("list_projects").paginate():
        for p in page["projects"]:
            if p["name"] == name:
                return p["arn"]
    return None


def _find_pool(client, project_arn: str, name: str) -> str | None:
    for page in client.get_paginator("list_device_pools").paginate(arn=project_arn):
        for p in page["devicePools"]:
            if p["name"] == name:
                return p["arn"]
    return None


def _discover(region: str) -> dict:
    """CDK 가 만든 프로젝트/풀을 이름으로 찾아 config 를 구성(+캐시)."""
    import boto3

    project_name = os.environ.get("DEVICEFARM_PROJECT", DEFAULT_PROJECT_NAME)
    pool_name = os.environ.get("DEVICEFARM_POOL", DEFAULT_POOL_NAME)
    client = boto3.client("devicefarm", region_name=region)

    project_arn = _find_project(client, project_name)
    if not project_arn:
        raise RuntimeError(
            f"Device Farm 프로젝트 '{project_name}' 를 {region} 에서 못 찾음. "
            "CDK 배포를 먼저 하거나(권장), 이름을 바꿨다면 DEVICEFARM_PROJECT env 로 지정하세요. "
            "(웹 탭만 쓸 거면 모바일 셋업은 불필요합니다.)")
    pool_arn = _find_pool(client, project_arn, pool_name)
    if not pool_arn:
        raise RuntimeError(
            f"Device Pool '{pool_name}' 를 프로젝트 '{project_name}' 에서 못 찾음. "
            "이름을 바꿨다면 DEVICEFARM_POOL env 로 지정하세요.")

    cfg = {
        "region": region,
        "projectName": project_name,
        "projectArn": project_arn,
        "devicePoolName": pool_name,
        "devicePoolArn": pool_arn,
    }
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    except OSError:
        pass  # 캐시 실패는 치명적이지 않음(매번 조회).
    return cfg


def resolve_config() -> dict:
    """모바일 경로 설정 딕셔너리 {region, projectArn, devicePoolArn, ...} 반환.

    우선순위(명시적 의도가 캐시를 이긴다):
      1) env ARN(DEVICEFARM_PROJECT_ARN) — 직접 지정.
      2) config.json 캐시. 단 env 이름 override(DEVICEFARM_PROJECT/POOL)와 다르면 stale 로
         보고 무시 → 재조회. (예: 고유 이름으로 재배포했는데 예전 config.json 이 남은 경우)
      3) 이름으로 Device Farm 조회(+캐시). CDK 배포만 했어도 여기서 채워진다.
    아무 것도 못 구하면 RuntimeError.
    """
    region = _region()

    # 1) env 로 직접 지정된 ARN 이 최우선.
    project_arn = os.environ.get("DEVICEFARM_PROJECT_ARN")
    if project_arn:
        return {
            "region": region,
            "projectArn": project_arn,
            "devicePoolArn": os.environ.get("DEVICEFARM_POOL_ARN"),
        }

    # 2) 캐시(config.json) — env 이름 override 와 충돌하면 무시(stale).
    want_project = os.environ.get("DEVICEFARM_PROJECT")
    want_pool = os.environ.get("DEVICEFARM_POOL")
    if CONFIG_PATH.is_file():
        cfg = json.loads(CONFIG_PATH.read_text())
        stale = (
            (want_project and cfg.get("projectName") != want_project)
            or (want_pool and cfg.get("devicePoolName") != want_pool)
        )
        if not stale:
            return cfg

    # 3) 이름으로 조회(+캐시).
    return _discover(region)
