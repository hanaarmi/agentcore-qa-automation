"""Device Farm config resolver (shared by the dashboard and run scripts).

config.json is an output produced by `devicefarm_setup.py`, not a value committed to
the repo. CDK deployment creates the project/pool but does not create this file, which
leaves a gap where the dashboard cannot find them. This resolver fills that gap. Priority:

  1) If infra/config.json exists, use it as-is.
  2) Otherwise, build from env (DEVICEFARM_PROJECT_ARN / DEVICEFARM_POOL_ARN).
  3) Otherwise, look up Device Farm by name. Names come from env
     (DEVICEFARM_PROJECT / DEVICEFARM_POOL) or the CDK defaults (qa-automation-demo /
     android-phones). If you changed the names with -c during CDK deployment, pass the
     same values via env.

When found by lookup, the result is cached to config.json to save API calls next time.
So after deploying alone, the mobile tab opens without manually running devicefarm_setup.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")

# Matches the CDK stack defaults (deploy/stacks/qa_automation_stack.py). If changed with -c, communicate via env.
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
    """Find the CDK-created project/pool by name and build the config (+cache)."""
    import boto3

    project_name = os.environ.get("DEVICEFARM_PROJECT", DEFAULT_PROJECT_NAME)
    pool_name = os.environ.get("DEVICEFARM_POOL", DEFAULT_POOL_NAME)
    client = boto3.client("devicefarm", region_name=region)

    project_arn = _find_project(client, project_name)
    if not project_arn:
        raise RuntimeError(
            f"Device Farm project '{project_name}' not found in {region}. "
            "Deploy with CDK first (recommended), or if you changed the name, set it via the DEVICEFARM_PROJECT env var. "
            "(If you only use the web tab, mobile setup is not needed.)")
    pool_arn = _find_pool(client, project_arn, pool_name)
    if not pool_arn:
        raise RuntimeError(
            f"Device Pool '{pool_name}' not found in project '{project_name}'. "
            "If you changed the name, set it via the DEVICEFARM_POOL env var.")

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
        pass  # Cache failure is not fatal (just look up every time).
    return cfg


def resolve_config() -> dict:
    """Return the mobile-path config dict {region, projectArn, devicePoolArn, ...}.

    Priority (explicit intent beats the cache):
      1) env ARN (DEVICEFARM_PROJECT_ARN) — direct override.
      2) config.json cache. But if it differs from the env name override
         (DEVICEFARM_PROJECT/POOL), treat it as stale and ignore it -> re-look up.
         (e.g. redeployed with a unique name but an old config.json remains.)
      3) Look up Device Farm by name (+cache). Populated here even after a CDK deploy alone.
    Raises RuntimeError if nothing can be resolved.
    """
    region = _region()

    # 1) An ARN directly specified via env takes top priority.
    project_arn = os.environ.get("DEVICEFARM_PROJECT_ARN")
    if project_arn:
        return {
            "region": region,
            "projectArn": project_arn,
            "devicePoolArn": os.environ.get("DEVICEFARM_POOL_ARN"),
        }

    # 2) Cache (config.json) — ignore it if it conflicts with the env name override (stale).
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

    # 3) Look up by name (+cache).
    return _discover(region)
