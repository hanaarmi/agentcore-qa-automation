"""Appium Python 테스트 zip 패키징.

우리가 생성한 Appium 테스트 파일(.py) 하나를 Device Farm custom mode 가 요구하는
zip 레이아웃으로 묶는다. **wheelhouse 불필요** — Device Farm 호스트가 requirements.txt 를
런타임에 pip install 한다.

zip 내부 레이아웃(루트 기준, 이게 어긋나면 pytest 가 테스트를 못 찾아 '0개 통과'로 거짓 green):
    requirements.txt
    tests/<testfile>.py

사용:
    python infra/package_appium.py out/happy_path_order_appium.py --out out/appium_pkg.zip
"""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

REQUIREMENTS = "Appium-Python-Client\npytest\nselenium\n"


def build(test_file: Path, out_zip: Path) -> None:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    # Device Farm 요건: 테스트 파일명이 "test" 로 시작하거나 끝나야 인식됨
    # (pytest 관례와 동일). 아니면 zip 검증에서 INVALID_TEST_FILE_NAME 로 반려.
    name = test_file.name
    if not (name.startswith("test") or name[:-3].endswith("test")):
        name = f"test_{name}"

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        # requirements.txt 는 zip 루트에.
        z.writestr("requirements.txt", REQUIREMENTS)
        # 테스트 파일은 tests/ 아래에, pytest 가 인식하는 이름으로.
        z.write(test_file, arcname=f"tests/{name}")
    # 레이아웃 검증(루트에 requirements.txt + tests/ 가 있는지).
    with zipfile.ZipFile(out_zip) as z:
        names = z.namelist()
    assert "requirements.txt" in names, f"requirements.txt not at root: {names}"
    assert any(n.startswith("tests/") and n.endswith(".py") for n in names), names
    print(f"wrote {out_zip}")
    print("layout:", names)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("test_file", type=Path, help="생성된 Appium .py 파일")
    ap.add_argument("--out", type=Path, default=Path("out/appium_pkg.zip"))
    args = ap.parse_args()
    if not args.test_file.is_file():
        raise SystemExit(f"not found: {args.test_file}")
    build(args.test_file, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
