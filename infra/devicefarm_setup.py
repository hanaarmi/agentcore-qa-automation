"""Device Farm one-time setup (IaC, idempotent).

Creates the project and Device Pool in code. Reuses them if they already exist (idempotent).
Saves the resulting ARNs to infra/config.json, which devicefarm_run.py reads.

Purpose: eliminate the repetitive task of picking devices in the console every time.

Usage:
    python infra/devicefarm_setup.py [--project NAME] [--pool NAME] [--region us-west-2]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import boto3

# The Device Farm control plane is us-west-2 only.
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
    """Device Pool pinned to physical Android devices. Encodes device selection as rules."""
    arn = _find_pool(client, project_arn, name)
    if arn:
        print(f"[pool] reuse: {name}")
        return arn

    # Rule-based: available physical Android devices. Max 1 device (demo simplification; use multiple runs for parallelism).
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
    from df_config import DEFAULT_PROJECT_NAME, DEFAULT_POOL_NAME
    ap.add_argument("--project", default=DEFAULT_PROJECT_NAME)
    ap.add_argument("--pool", default=DEFAULT_POOL_NAME)
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
