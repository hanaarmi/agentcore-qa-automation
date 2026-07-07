"""Device Farm 실행 (매번): upload -> schedule-run -> poll -> 아티팩트 수집.

콘솔 클릭 0회. APK와 테스트 패키지를 업로드하고 실행한 뒤, 완료까지 폴링하고
결과/영상/로그를 로컬로 내려받는다.

사용:
    python infra/devicefarm_run.py \
        --apk path/to/app-debug.apk \
        --tests path/to/appium_tests.zip \
        --type APPIUM_PYTHON \
        [--run-name demo] [--out artifacts/]

주의:
- Device Farm 업로드는 presigned S3 PUT 방식이다: create_upload 로 URL을 받아 파일을 PUT하고,
  status 가 SUCCEEDED 될 때까지 기다려야 schedule_run 에서 쓸 수 있다.
- 이 스크립트는 blocking 폴링이지만 LLM을 호출하지 않는다(실행 루프에 LLM 없음).
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import boto3
import requests  # presigned URL PUT 용

POLL_SECONDS = 15


def load_config() -> dict:
    # config.json → env → CDK 가 만든 프로젝트/풀 이름 조회 순(df_config).
    from df_config import resolve_config
    try:
        return resolve_config()
    except RuntimeError as e:
        raise SystemExit(str(e))


def _upload_type_for(test_type: str, is_app: bool) -> str:
    if is_app:
        return "ANDROID_APP"
    # 테스트 패키지 업로드 타입 매핑.
    return {
        "APPIUM_PYTHON": "APPIUM_PYTHON_TEST_PACKAGE",
        "APPIUM_NODE": "APPIUM_NODE_TEST_PACKAGE",
        "INSTRUMENTATION": "INSTRUMENTATION_TEST_PACKAGE",
    }.get(test_type, "APPIUM_PYTHON_TEST_PACKAGE")


def create_and_wait_upload(client, project_arn: str, file_path: Path, upload_type: str) -> str:
    """create_upload -> presigned PUT -> SUCCEEDED 까지 폴링. 업로드 ARN 반환."""
    up = client.create_upload(
        projectArn=project_arn,
        name=file_path.name,
        type=upload_type,
    )["upload"]
    arn, url = up["arn"], up["url"]

    print(f"[upload] PUT {file_path.name} ({upload_type}) ...")
    resp = requests.put(url, data=file_path.read_bytes())
    resp.raise_for_status()

    # 업로드가 서버에서 처리(SUCCEEDED)될 때까지 대기.
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
    # BUILTIN_FUZZ 는 테스트 패키지가 필요 없다(앱만 있으면 기기가 자동 조작).
    test_spec: dict = {"type": test_type}
    if test_arn:
        test_spec["testPackageArn"] = test_arn
    # custom mode: testSpecArn 가 있으면 우리 testspec.yml 로 실행(권장 방식).
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

    # 완료(COMPLETED)까지 폴링. 진행 상태를 그대로 출력.
    while True:
        run = client.get_run(arn=run_arn)["run"]
        status = run["status"]
        print(f"[run] status={status}")
        if status == "COMPLETED":
            print(f"[run] result={run.get('result')}")
            return run
        time.sleep(POLL_SECONDS)


def collect_artifacts(client, run_arn: str, out_dir: Path) -> None:
    """run 의 job/suite/test 아티팩트(영상/로그 등)를 내려받는다."""
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
                    help="테스트 패키지(zip). BUILTIN_FUZZ 에서는 불필요.")
    ap.add_argument("--type", default="APPIUM_PYTHON", dest="test_type",
                    help="APPIUM_PYTHON | BUILTIN_FUZZ | INSTRUMENTATION ...")
    ap.add_argument("--test-spec", type=Path, default=None,
                    help="testspec.yml (custom mode). APPIUM_PYTHON 권장.")
    ap.add_argument("--run-name", default="qa-demo-run")
    ap.add_argument("--out", type=Path, default=Path("artifacts"))
    args = ap.parse_args()

    cfg = load_config()
    client = boto3.client("devicefarm", region_name=cfg["region"])

    app_arn = create_and_wait_upload(
        client, cfg["projectArn"], args.apk, _upload_type_for(args.test_type, is_app=True)
    )

    # BUILTIN_FUZZ 는 테스트 패키지가 필요 없다(앱만 올리면 기기가 자동 조작 + 영상/로그 생성).
    test_arn = None
    if args.test_type != "BUILTIN_FUZZ":
        if not args.tests:
            raise SystemExit(f"--type {args.test_type} 은 --tests 패키지가 필요합니다.")
        test_arn = create_and_wait_upload(
            client, cfg["projectArn"], args.tests, _upload_type_for(args.test_type, is_app=False)
        )

    # custom mode: testspec.yml 업로드(있으면).
    test_spec_arn = None
    if args.test_spec:
        if not args.test_spec.is_file():
            raise SystemExit(f"test spec 없음: {args.test_spec}")
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
