"""Device Farm run (every time): upload -> schedule-run -> poll -> collect artifacts.

No console clicks. Uploads the APK and test package, runs them, polls until
completion, and downloads the results/videos/logs locally.

Usage:
    python infra/devicefarm_run.py \
        --apk path/to/app-debug.apk \
        --tests path/to/appium_tests.zip \
        --type APPIUM_PYTHON \
        [--run-name demo] [--out artifacts/]

Notes:
- Device Farm uploads use presigned S3 PUT: call create_upload to get a URL, PUT the
  file, and wait until status is SUCCEEDED before it can be used in schedule_run.
- This script uses blocking polling but does not call an LLM (no LLM in the run loop).
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import boto3
import requests  # for presigned URL PUT

POLL_SECONDS = 15


def load_config() -> dict:
    # Resolution order: config.json -> env -> lookup of CDK-created project/pool names (df_config).
    from df_config import resolve_config
    try:
        return resolve_config()
    except RuntimeError as e:
        raise SystemExit(str(e))


def _upload_type_for(test_type: str, is_app: bool) -> str:
    if is_app:
        return "ANDROID_APP"
    # Test package upload type mapping.
    return {
        "APPIUM_PYTHON": "APPIUM_PYTHON_TEST_PACKAGE",
        "APPIUM_NODE": "APPIUM_NODE_TEST_PACKAGE",
        "INSTRUMENTATION": "INSTRUMENTATION_TEST_PACKAGE",
    }.get(test_type, "APPIUM_PYTHON_TEST_PACKAGE")


def create_and_wait_upload(client, project_arn: str, file_path: Path, upload_type: str) -> str:
    """create_upload -> presigned PUT -> poll until SUCCEEDED. Returns the upload ARN."""
    up = client.create_upload(
        projectArn=project_arn,
        name=file_path.name,
        type=upload_type,
    )["upload"]
    arn, url = up["arn"], up["url"]

    print(f"[upload] PUT {file_path.name} ({upload_type}) ...")
    resp = requests.put(url, data=file_path.read_bytes())
    resp.raise_for_status()

    # Wait until the upload is processed on the server side (SUCCEEDED).
    while True:
        u = client.get_upload(arn=arn)["upload"]
        status = u["status"]
        if status == "SUCCEEDED":
            print(f"[upload] {file_path.name}: SUCCEEDED")
            return arn
        if status == "FAILED":
            raise SystemExit(f"[upload] {file_path.name}: FAILED — {u.get('message')}")
        time.sleep(3)


def schedule_and_wait_run(
    client, project_arn: str, pool_arn: str, app_arn: str, test_arn: str | None,
    test_type: str, run_name: str, test_spec_arn: str | None = None,
) -> dict:
    # BUILTIN_FUZZ needs no test package (the device drives the app automatically).
    test_spec: dict = {"type": test_type}
    if test_arn:
        test_spec["testPackageArn"] = test_arn
    # custom mode: when testSpecArn is present, run with our testspec.yml (recommended).
    if test_spec_arn:
        test_spec["testSpecArn"] = test_spec_arn
    run_arn = client.schedule_run(
        projectArn=project_arn,
        appArn=app_arn,
        devicePoolArn=pool_arn,
        name=run_name,
        test=test_spec,
    )["run"]["arn"]
    print(f"[run] scheduled: {run_name}  (arn={run_arn.split('/')[-1]})")

    # Poll until COMPLETED. Print the progress status as-is.
    while True:
        run = client.get_run(arn=run_arn)["run"]
        status = run["status"]
        print(f"[run] status={status}")
        if status == "COMPLETED":
            print(f"[run] result={run.get('result')}")
            return run
        time.sleep(POLL_SECONDS)


def collect_artifacts(client, run_arn: str, out_dir: Path) -> None:
    """Download the run's job/suite/test artifacts (videos, logs, etc.)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for atype in ("FILE", "LOG", "SCREENSHOT"):
        arts = client.list_artifacts(arn=run_arn, type=atype)["artifacts"]
        for a in arts:
            url = a.get("url")
            if not url:
                continue
            ext = a.get("extension", "")
            fname = f"{a['name']}-{a['arn'].split('/')[-1][:8]}.{ext}".replace("/", "_")
            dest = out_dir / fname
            r = requests.get(url)
            if r.ok:
                dest.write_bytes(r.content)
                print(f"[artifact] {dest.name}")
    print(f"[artifact] saved to {out_dir}/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", type=Path, required=True)
    ap.add_argument("--tests", type=Path, default=None,
                    help="Test package (zip). Not required for BUILTIN_FUZZ.")
    ap.add_argument("--type", default="APPIUM_PYTHON", dest="test_type",
                    help="APPIUM_PYTHON | BUILTIN_FUZZ | INSTRUMENTATION ...")
    ap.add_argument("--test-spec", type=Path, default=None,
                    help="testspec.yml (custom mode). Recommended for APPIUM_PYTHON.")
    ap.add_argument("--run-name", default="qa-demo-run")
    ap.add_argument("--out", type=Path, default=Path("artifacts"))
    args = ap.parse_args()

    cfg = load_config()
    client = boto3.client("devicefarm", region_name=cfg["region"])

    app_arn = create_and_wait_upload(
        client, cfg["projectArn"], args.apk, _upload_type_for(args.test_type, is_app=True)
    )

    # BUILTIN_FUZZ needs no test package (upload the app and the device drives it automatically, producing video/logs).
    test_arn = None
    if args.test_type != "BUILTIN_FUZZ":
        if not args.tests:
            raise SystemExit(f"--type {args.test_type} requires a --tests package.")
        test_arn = create_and_wait_upload(
            client, cfg["projectArn"], args.tests, _upload_type_for(args.test_type, is_app=False)
        )

    # custom mode: upload testspec.yml (if provided).
    test_spec_arn = None
    if args.test_spec:
        if not args.test_spec.is_file():
            raise SystemExit(f"test spec not found: {args.test_spec}")
        test_spec_arn = create_and_wait_upload(
            client, cfg["projectArn"], args.test_spec, "APPIUM_PYTHON_TEST_SPEC"
        )

    run = schedule_and_wait_run(
        client, cfg["projectArn"], cfg["devicePoolArn"],
        app_arn, test_arn, args.test_type, args.run_name, test_spec_arn,
    )
    collect_artifacts(client, run["arn"], args.out)

    ok = run.get("result") == "PASSED"
    print("\n=== RESULT:", run.get("result"), "===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
