# PyInstaller spec for Magoo. Build with packaging/build.ps1.
#
# onedir, not onefile: onefile re-extracts the whole tree to a temp directory
# on every launch (slow start for a NumPy/SciPy app), and its self-extracting
# stub is the shape antivirus heuristics like least.
#
#     .venv/Scripts/pyinstaller.exe packaging/magoo.spec --noconfirm
#
# Build from a CLEAN virtualenv. The build environment dominates the output
# size far more than anything in this file.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT = Path(SPECPATH).resolve().parent

datas = [
    # Jinja loads these off disk at render time; PyInstaller does not collect
    # .html on its own, and without them every page is a TemplateNotFound.
    (str(PROJECT / "magoo" / "templates"), "magoo/templates"),
]
# httpx verifies TLS against certifi's bundle — needed for ESI and the SDE.
datas += collect_data_files("certifi")

hiddenimports = [
    # SciPy reaches its solver backends dynamically, so static analysis
    # cannot see them. Missing, they fail at RUNTIME the first time someone
    # plans a run — a build that starts up fine can still be useless.
    # NOTE the module names track the pinned SciPy: as of 1.18 the HiGHS
    # backend behind milp() is _highspy (it was _highs in older releases),
    # and scipy.special._cdflib no longer exists. Naming a module that is
    # not there is not harmless — PyInstaller logs it as ERROR, which is
    # exactly how a real missing import gets lost in the noise.
    "scipy.optimize._highspy",
    "scipy.optimize._highspy._core",
    "scipy.optimize._linprog_highs",
    "scipy._lib.messagestream",
    "scipy.special._ufuncs_cxx",
    # pywebview's Windows backend goes through pythonnet, whose loader is
    # the single flakiest link in this stack.
    "clr_loader",
    "pythonnet",
    "webview.platforms.edgechromium",
]
hiddenimports += collect_submodules("scipy.optimize")

excludes = [
    # pywebview pulls in every backend whether or not it is used.
    "webview.platforms.cocoa",
    "webview.platforms.gtk",
    "webview.platforms.qt",
    "webview.platforms.android",
    # Never imported by Magoo; all of them are large.
    "tkinter",
    "matplotlib",
    "PIL",
    "pytest",
    "setuptools",
    "pip",
    "IPython",
    "notebook",
    "pandas",
]

a = Analysis(
    [str(PROJECT / "packaging" / "entry.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Magoo",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX measurably worsens antivirus false positives, and PyInstaller uses
    # it silently whenever it happens to be on PATH.
    upx=False,
    # No console: this is a desktop app. logsetup handles the consequence —
    # sys.stdout is None in this mode, which is why nothing may rely on
    # print() succeeding.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT / "packaging" / "magoo.ico"),
    version=str(PROJECT / "packaging" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Magoo",
)
