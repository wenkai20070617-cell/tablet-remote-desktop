from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
NAME = "tablet-remote-desktop"


def run() -> None:
    separator = os.pathsep
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--name",
            NAME,
            "--add-data",
            f"{ROOT / 'app' / 'static'}{separator}app/static",
            "--collect-all",
            "aiortc",
            "--collect-all",
            "av",
            str(ROOT / "launcher.py"),
        ],
        cwd=ROOT,
        check=True,
    )
    system = platform.system().lower()
    machine = platform.machine().lower().replace("amd64", "x86_64")
    source = DIST / NAME
    archive_base = DIST / f"{NAME}-{system}-{machine}"
    if system == "windows":
        archive = Path(shutil.make_archive(str(archive_base), "zip", DIST, NAME))
    else:
        archive = archive_base.with_suffix(".tar.gz")
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(source, arcname=NAME)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(archive)
    print(checksum)


if __name__ == "__main__":
    run()