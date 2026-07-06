"""대시보드용 Device Farm 라이브 상태 어댑터.

시뮬레이션이 아니라 **실제 Device Farm run**의 상태와 완료 영상을 가져온다.
- run 을 새로 스케줄하지 않고(과금/시간), 이미 존재하는 run(또는 최신 run)의 상태를 조회.
- Device Farm 은 라이브 비디오 스트림을 표준 제공하지 않으므로: 진행 중엔 상태/카운터를,
  완료 후엔 녹화 영상(VIDEO) 아티팩트 URL 을 제공한다. (ISSUE-004 / D-011)

환경변수:
    DEVICEFARM_PROJECT_ARN   조회할 프로젝트 (없으면 config.json 사용)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import boto3

_CONFIG = Path(__file__).resolve().parent.parent / "infra" / "config.json"


def _client_and_project():
    region = os.environ.get("AGENTCORE_REGION", "us-west-2")
    project = os.environ.get("DEVICEFARM_PROJECT_ARN")
    if not project and _CONFIG.is_file():
        cfg = json.loads(_CONFIG.read_text())
        project = cfg["projectArn"]
        region = cfg.get("region", region)
    if not project:
        raise RuntimeError("Device Farm 프로젝트 ARN 없음 (config.json 또는 env 필요)")
    return boto3.client("devicefarm", region_name=region), project


def list_runs(limit: int = 10) -> list[dict]:
    """프로젝트의 최근 run 목록 + 상태/카운터."""
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
    """완료된 run 의 녹화 영상(VIDEO) 아티팩트 URL 들."""
    client, _ = _client_and_project()
    urls = []
    arts = client.list_artifacts(arn=run_arn, type="FILE")["artifacts"]
    for a in arts:
        if a.get("type") == "VIDEO" and a.get("url"):
            urls.append(a["url"])
    return urls
