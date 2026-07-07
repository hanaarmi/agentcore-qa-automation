"""Appium Python test zip packaging.

Bundles a single generated Appium test file (.py) into the zip layout that Device Farm
custom mode requires. No wheelhouse needed — the Device Farm host runs pip install on
requirements.txt at runtime.

Zip layout (relative to root; if this is wrong, pytest cannot find the tests and reports
a false green with '0 passed'):
    requirements.txt
    tests/<testfile>.py

Usage:
    python infra/package_appium.py out/happy_path_order_appium.py --out out/appium_pkg.zip
"""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

REQUIREMENTS = "Appium-Python-Client\npytest\nselenium\n"


def build(test_file: Path, out_zip: Path) -> None:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    # Device Farm requirement: the test file name must start or end with "test" to be
    # recognized (same as the pytest convention). Otherwise the zip validation rejects it
    # with INVALID_TEST_FILE_NAME.
    name = test_file.name
    if not (name.startswith("test") or name[:-3].endswith("test")):
        name = f"test_{name}"

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        # requirements.txt goes at the zip root.
        z.writestr("requirements.txt", REQUIREMENTS)
        # The test file goes under tests/, with a name pytest recognizes.
        z.write(test_file, arcname=f"tests/{name}")
    # Validate the layout (requirements.txt at root + a tests/ file).
    with zipfile.ZipFile(out_zip) as z:
        names = z.namelist()
    assert "requirements.txt" in names, f"requirements.txt not at root: {names}"
    assert any(n.startswith("tests/") and n.endswith(".py") for n in names), names
    print(f"wrote {out_zip}")
    print("layout:", names)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("test_file", type=Path, help="Generated Appium .py file")
    ap.add_argument("--out", type=Path, default=Path("out/appium_pkg.zip"))
    args = ap.parse_args()
    if not args.test_file.is_file():
        raise SystemExit(f"not found: {args.test_file}")
    build(args.test_file, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
