"""Device Farm live-status adapter for the dashboard.

Fetches the status and completed recording of a **real Device Farm run**, not a
simulation.
- Does not schedule a new run (to avoid cost/time); queries the status of an
  existing run (or the latest run).
- Device Farm does not offer a standard live video stream, so: during a run it
  exposes status/counters, and after completion it exposes the recorded video
  (VIDEO) artifact URLs.

Environment variables:
    DEVICEFARM_PROJECT_ARN   project to query (falls back to config.json, then
                             name lookup)
"""
from __future__ import annotations

import sys
from pathlib import Path

import boto3

# Reuse the shared config resolver from infra/ (config.json -> env -> name lookup).
_INFRA = Path(__file__).resolve().parent.parent / "infra"
if str(_INFRA) not in sys.path:
    sys.path.insert(0, str(_INFRA))
from df_config import resolve_config  # noqa: E402


def _client_and_project():
    cfg = resolve_config()
    region = cfg.get("region", "us-west-2")
    return boto3.client("devicefarm", region_name=region), cfg["projectArn"]


def list_runs(limit: int = 10) -> list[dict]:
    """Recent runs for the project, with status/counters."""
    client, project = _client_and_project()
    runs = client.list_runs(arn=project)["runs"][:limit]
    return [_public_run(r) for r in runs]


def _public_run(r: dict) -> dict:
    c = r.get("counters", {}) or {}
    return {
        "arn": r["arn"],
        "name": r.get("name"),
        "status": r.get("status"),      # SCHEDULING/RUNNING/COMPLETED ...
        "result": r.get("result"),      # PENDING/PASSED/FAILED ...
        "platform": r.get("platform"),
        "device": (r.get("device") or {}).get("name"),
        "counters": {
            "total": c.get("total", 0),
            "passed": c.get("passed", 0),
            "failed": c.get("failed", 0),
        },
    }


def run_video_urls(run_arn: str) -> list[str]:
    """Recorded video (VIDEO) artifact URLs for a completed run."""
    client, _ = _client_and_project()
    urls = []
    arts = client.list_artifacts(arn=run_arn, type="FILE")["artifacts"]
    for a in arts:
        if a.get("type") == "VIDEO" and a.get("url"):
            urls.append(a["url"])
    return urls
