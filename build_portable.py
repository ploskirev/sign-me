"""Build a self-contained macOS arm64 command; never include local patient files."""

import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / ".build"
RELEASE = ROOT / "release"


def main():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("This build targets macOS arm64 and must run on an Apple Silicon Mac.")
    BUILD.mkdir(exist_ok=True)
    RELEASE.mkdir(exist_ok=True)
    staging = BUILD / "release-staging"
    staging.mkdir(exist_ok=True)
    chromium = ROOT / ".browsers" / "chromium-1194"
    if not chromium.is_dir():
        raise SystemExit("Install the Chromium revision matching requirements.txt before building.")
    archive = BUILD / "chromium.tar"
    print("Packing the bundled Chromium…", flush=True)
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as output:
        output.add(chromium, arcname=chromium.name)
    environment = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(BUILD / "pyinstaller-cache"))
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
                    "--name", "Запись.command", "--distpath", str(staging), "--workpath", str(BUILD / "work"),
                    "--specpath", str(BUILD), "--collect-all", "playwright", "--noupx",
                    "--add-data", f"{archive}:.", str(ROOT / "talon.py")], cwd=ROOT, env=environment, check=True)
    # Publish only a complete executable; an already-running copy keeps its old inode.
    staged_binary = staging / "Запись.command"
    staged_binary.chmod(0o755)
    os.replace(staged_binary, RELEASE / "Запись.command")
    for filename in ("config.txt", "README.txt"):
        target = RELEASE / filename
        if filename == "config.txt" and target.exists():
            print("Existing release/config.txt preserved.", flush=True)
        else:
            shutil.copy2(ROOT / "portable" / filename, target)
    binary = RELEASE / "Запись.command"
    binary.chmod(0o755)
    print(f"Built {binary} ({binary.stat().st_size / 1024**2:.1f} MiB).", flush=True)


if __name__ == "__main__":
    main()
