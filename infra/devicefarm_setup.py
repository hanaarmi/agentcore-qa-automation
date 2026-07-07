"""Device Farm 1회 셋업 (IaC, 멱등).

프로젝트와 Device Pool을 코드로 생성한다. 이미 있으면 재사용(멱등).
결과 ARN을 infra/config.json 에 저장 → devicefarm_run.py 가 읽는다.

목적: 매번 콘솔에서 기기를 고르는 반복 작업 제거.

사용:
    python infra/devicefarm_setup.py [--project NAME] [--pool NAME] [--region us-west-2]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import boto3

# Device Farm 컨트롤 플레인은 us-west-2 전용.
DEFAULT_REGION = "us-west-2"
CONFIG_PATH = Path(__file__).with_name("config.json")


def _find_project(client, name: str) -> str | None:
    paginator = client.get_paginator("list_projects")
    for page in paginator.paginate():
        for p in page["projects"]:
            if p["name"] == name:
                return p["arn"]
    return None


def ensure_project(client, name: str) -> str:
    arn = _find_project(client, name)
    if arn:
        print(f"[project] reuse: {name}")
        return arn
    arn = client.create_project(name=name)["project"]["arn"]
    print(f"[project] created: {name}")
    return arn


def _find_pool(client, project_arn: str, name: str) -> str | None:
    paginator = client.get_paginator("list_device_pools")
    for page in paginator.paginate(arn=project_arn):
        for p in page["devicePools"]:
            if p["name"] == name:
                return p["arn"]
    return None


def ensure_device_pool(client, project_arn: str, name: str) -> str:
    """Android 실기기로 고정된 Device Pool. 기기 선택을 규칙으로 코드화."""
    arn = _find_pool(client, project_arn, name)
    if arn:
        print(f"[pool] reuse: {name}")
        return arn

    # 규칙 기반: 물리 Android 기기 중 가용한 것. 최대 1대(데모 단순화; 병렬은 run 여러 개로).
    rules = [
        {"attribute": "PLATFORM", "operator": "EQUALS", "value": '"ANDROID"'},
        {"attribute": "FORM_FACTOR", "operator": "EQUALS", "value": '"PHONE"'},
        {"attribute": "AVAILABILITY", "operator": "EQUALS", "value": '"HIGHLY_AVAILABLE"'},
    ]
    arn = client.create_device_pool(
        projectArn=project_arn,
        name=name,
        description="Android phones, highly available (demo)",
        rules=rules,
        maxDevices=1,
    )["devicePool"]["arn"]
    print(f"[pool] created: {name}")
    return arn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="qa-automation-demo")
    ap.add_argument("--pool", default="android-phones")
    ap.add_argument("--region", default=DEFAULT_REGION)
    args = ap.parse_args()

    client = boto3.client("devicefarm", region_name=args.region)

    project_arn = ensure_project(client, args.project)
    pool_arn = ensure_device_pool(client, project_arn, args.pool)

    config = {
        "region": args.region,
        "projectName": args.project,
        "projectArn": project_arn,
        "devicePoolName": args.pool,
        "devicePoolArn": pool_arn,
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    print(f"\nwrote {CONFIG_PATH}")
    print(json.dumps(config, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
