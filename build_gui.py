"""Build a self-contained Finder application for macOS Apple Silicon."""
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / '.build'
NAME = 'Запись к врачу'


def build_icon():
    source = ROOT / 'gui_assets' / 'app-icon.png'
    iconset = BUILD / 'AppIcon.iconset'
    iconset.mkdir(exist_ok=True)
    # Generate each macOS scale from the original, avoiding repeated resampling.
    for size in (16,32,128,256,512):
        for scale in (1,2):
            suffix = '@2x' if scale == 2 else ''
            target = iconset / f'icon_{size}x{size}{suffix}.png'
            pixels = str(size*scale)
            subprocess.run(['sips','-z',pixels,pixels,str(source),'--out',str(target)],
                           check=True,stdout=subprocess.DEVNULL)
    icon = BUILD / 'AppIcon.icns'
    subprocess.run(['iconutil','-c','icns',str(iconset),'-o',str(icon)],check=True)
    return icon


def main():
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise SystemExit('Build on an Apple Silicon Mac.')
    # Fail early if the build environment prevents Qt from inspecting the CPU.
    # Otherwise PyInstaller can silently omit the macOS platform plugin.
    subprocess.run([sys.executable,'-c',
        'from PySide6.QtCore import QCoreApplication, QLibraryInfo; app=QCoreApplication([]); print(QLibraryInfo.path(QLibraryInfo.PluginsPath))'],check=True)
    BUILD.mkdir(exist_ok=True)
    icon = build_icon()
    native = BUILD / 'native' / 'libtalon_backdrop.dylib'
    native.parent.mkdir(exist_ok=True)
    subprocess.run(['xcrun','clang','-dynamiclib','-fobjc-arc','-framework','Cocoa',
        '-mmacosx-version-min=12.0',str(ROOT/'native/macos_backdrop.m'),'-o',str(native)],check=True)
    archive = BUILD / 'chromium.tar'
    if not archive.exists():
        with tarfile.open(archive,'w',format=tarfile.PAX_FORMAT) as output:
            output.add(ROOT/'.browsers'/'chromium-1194',arcname='chromium-1194')
    staging = BUILD / 'gui-staging'
    environment = dict(os.environ,PYINSTALLER_CONFIG_DIR=str(BUILD/'pyinstaller-cache-gui'))
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--windowed',
        '--name',NAME,'--distpath',str(staging),'--workpath',str(BUILD/'gui-work'),
        '--icon',str(icon),
        '--specpath',str(BUILD),'--collect-all','playwright','--noupx','--add-data',f'{archive}:.',
        '--add-data',f'{ROOT / "gui_assets"}:gui_assets',
        '--add-binary',f'{native}:.',
        '--osx-bundle-identifier','local.talon.booking',str(ROOT/'talon_gui.py')],cwd=ROOT,env=environment,check=True)
    release = ROOT / 'release_gui'
    release.mkdir(exist_ok=True)
    destination = release / (NAME+'.app')
    if destination.exists():
        backup = BUILD / (NAME+f'-previous-{time.time_ns()}.app')
        destination.rename(backup)
    (staging/(NAME+'.app')).rename(destination)
    shutil.copy2(ROOT/'GUI_README.txt',release/'Как пользоваться.txt')
    print(f'Built {destination}',flush=True)


if __name__ == '__main__':
    main()
