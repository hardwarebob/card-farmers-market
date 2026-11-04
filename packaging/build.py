"""Builds Card Farm's downloads into dist/:

  python packaging/build.py linux     card-farm.pyz (runs with the system's Python and GTK)
  python packaging/build.py windows   Card-Farm-Windows.zip ("Card Farm" folder with Card Farm.exe)
  python packaging/build.py macos     Card-Farm-macOS.zip (Card Farm.app; for both kinds of Mac when built
                                      with python.org's universal2 Python, else -Intel on an Intel Mac)

Windows and macOS need `pip install pyinstaller pywebview pillow` first. Every download carries
THIRD-PARTY-NOTICES.txt: the licences of Python and of each library bundled into it (the .pyz
bundles nothing; it uses what the system has)."""
import importlib.metadata as md
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import zipapp

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD_ONLY = {"pyinstaller", "pyinstaller-hooks-contrib", "pip", "setuptools", "wheel", "pillow", "altgraph",
              "macholib", "pefile", "pywin32-ctypes", "packaging", "pytest", "pluggy", "iniconfig", "pygments",
              "colorama", "card-farm"}


def notices():
    """Licence texts of Python and of the libraries the build bundles."""
    parts = ["Card Farmer's Market is MIT licensed (see LICENSE). It is built with, and its downloads contain, the\n"
             "following software under their own licences.\n"]
    python_licence = next((p for p in (pathlib.Path(sys.base_prefix, "LICENSE.txt"),
                                       pathlib.Path(sys.base_prefix, "lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "LICENSE.txt"))
                           if p.exists()), None)
    parts.append(f"=== Python {sys.version.split()[0]} (PSF License) ===\n"
                 + (python_licence.read_text(errors="replace") if python_licence else "https://docs.python.org/3/license.html\n"))
    parts.append("=== PyInstaller bootloader ===\nGPL 2.0 with an exception that allows distributing programs built with it\n"
                 "under any licence: https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt\n")
    for dist in sorted(md.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        if name.lower() in BUILD_ONLY:
            continue
        texts = [t for t in (_licence_text(f) for f in dist.files or []) if t]
        summary = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
        parts.append(f"=== {name} {dist.version} ({summary.splitlines()[0] if summary else 'see text'}) ===\n"
                     + ("\n".join(texts) if texts else f"{dist.metadata.get('Home-page') or ''}\n"))
    return "\n\n".join(parts)


def _licence_text(f):
    """The text of a licence file a library ships, or None for anything else (some libraries have
    code or binaries whose names start with LICENSE)."""
    if not f.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")) or \
            f.suffix.lower() in (".py", ".pyc", ".so", ".dylib", ".dll", ".pyd"):
        return None
    try:
        data = f.locate().read_bytes()
    except OSError:
        return None
    return None if b"\0" in data else data.decode("utf-8", "replace")


def linux():
    stage = DIST / "pyz"
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(ROOT / "cardfarm", stage / "cardfarm", ignore=shutil.ignore_patterns("__pycache__"))
    (stage / "__main__.py").write_text("from cardfarm.__main__ import main\n\nmain()\n")
    zipapp.create_archive(stage, DIST / "card-farm.pyz", interpreter="/usr/bin/env python3", compressed=True)
    shutil.rmtree(stage)
    return DIST / "card-farm.pyz"


NAME = "Card Farmer's Market"  # what people see; the files keep "Card Farm" so older copies can update


def version():
    import tomllib
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def windows_details():
    """The Windows program details (taskbar, Task Manager, file properties) with the shown name."""
    numbers = tuple(int(n) for n in version().split(".")) + (0,)
    path = ROOT / "build" / "version-info.txt"
    path.parent.mkdir(exist_ok=True)
    fields = {"CompanyName": "m4ybe", "FileDescription": NAME, "FileVersion": version(), "InternalName": "Card Farm",
              "OriginalFilename": "Card Farm.exe", "ProductName": NAME, "ProductVersion": version(),
              "LegalCopyright": "MIT License"}
    strings = ", ".join(f"StringStruct({k!r}, {v!r})" for k, v in fields.items())
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [{strings}])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""")
    return path


def pyinstaller(icon, extra=()):
    sep = ";" if sys.platform == "win32" else ":"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "Card Farm", *extra,
                    "--icon", str(icon), "--add-data", f"{ROOT / 'cardfarm' / 'ui'}{sep}cardfarm/ui",
                    "--add-data", f"{ROOT / 'LICENSE'}{sep}.", "--osx-bundle-identifier", "io.github.cardfarm.CardFarm",
                    "--distpath", str(DIST), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
                    str(ROOT / "packaging" / "card_farm.py")], check=True, cwd=ROOT)


def windows():
    pyinstaller(ROOT / "packaging" / "icon.png", ["--version-file", str(windows_details())])
    folder = DIST / "Card Farm"
    (folder / "THIRD-PARTY-NOTICES.txt").write_text(notices(), encoding="utf-8")
    shutil.copy(ROOT / "LICENSE", folder / "LICENSE.txt")
    return shutil.make_archive(str(DIST / "Card-Farm-Windows"), "zip", DIST, "Card Farm")


def macos():
    import plistlib
    universal = {"x86_64", "arm64"} <= _archs(sys.executable)
    pyinstaller(ROOT / "packaging" / "icon.png", ["--target-arch", "universal2"] if universal else [])
    app = DIST / "Card Farm.app"
    if universal:
        _check_universal(app)
    info = app / "Contents" / "Info.plist"  # the shown name (menu bar, About) and version
    plist = plistlib.loads(info.read_bytes())
    plist.update(CFBundleName=NAME, CFBundleDisplayName=NAME, CFBundleShortVersionString=version(), CFBundleVersion=version())
    info.write_bytes(plistlib.dumps(plist))
    (app / "Contents" / "Resources" / "THIRD-PARTY-NOTICES.txt").write_text(notices())
    # sign again after every change to the bundle (a change after signing breaks it on Apple silicon)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    out = DIST / ("Card-Farm-macOS.zip" if universal or platform.machine() != "x86_64" else "Card-Farm-macOS-Intel.zip")
    subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(out)], check=True)  # keeps the app's links and signature
    if universal:  # Intel Macs on 0.3.0-beta.9 and older only look for the -Intel name
        shutil.copy(out, DIST / "Card-Farm-macOS-Intel.zip")
    return out


def _archs(path):
    """The processor kinds a Mach-O file has code for (empty for anything else)."""
    with open(path, "rb") as f:
        if f.read(4) not in (b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
            return set()
    done = subprocess.run(["lipo", "-archs", str(path)], capture_output=True, text=True)
    return set(done.stdout.split()) if done.returncode == 0 else set()


def _check_universal(app):
    """Every program file in the app has Apple silicon and Intel code: the build machine can only
    run one of them, so this is what shows the other is there."""
    thin = [str(p.relative_to(app)) for p in app.rglob("*")
            if p.is_file() and not p.is_symlink() and (a := _archs(p)) and not {"x86_64", "arm64"} <= a]
    if thin:
        sys.exit("not universal: " + ", ".join(thin))
    print("every program file in the app runs on Apple silicon and Intel")


if __name__ == "__main__":
    DIST.mkdir(exist_ok=True)
    print({"linux": linux, "windows": windows, "macos": macos}[sys.argv[1]]())
