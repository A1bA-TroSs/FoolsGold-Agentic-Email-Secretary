# PyInstaller spec for the frozen backend. Built by scripts/build_backend.sh.
#
# Why freeze at all: the packaged app used to ship backend/.venv, whose python
# is a symlink into the developer's Homebrew. It would have run on one Mac.
# A frozen one-folder build carries its own interpreter and every compiled
# module, and electron-builder signs each file in it along with the app.
#
# One-folder, not one-file: a one-file build unpacks itself to a temp dir on
# every launch, which is slower, leaves a signed bundle executing unsigned
# copies, and is what notarisation logs complain about.
from PyInstaller.utils.hooks import collect_submodules

hidden = (
    # The app's own modules: several are imported inside functions (routers
    # pull sources and providers lazily), and a missed one surfaces only when
    # that code path first runs -- in a user's hands, not in this build.
    collect_submodules("app")
    # uvicorn picks its loop and protocol implementations by name at run time.
    + [
        "uvicorn.logging",
        "uvicorn.loops.auto", "uvicorn.loops.asyncio", "uvicorn.loops.uvloop",
        "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    ]
)

a = Analysis(
    ["run_backend.py"],
    pathex=["."],
    hiddenimports=hidden,
    # Test tooling is in requirements.txt for development; it has no business
    # in a shipped binary.
    excludes=["pytest", "_pytest", "pytest_asyncio", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="foolsgold-backend",
    console=True,           # it is a server; stdout/stderr go to the Electron shell
    strip=False,
    upx=False,              # UPX-packed binaries break code signing
    codesign_identity=None, # electron-builder signs the whole bundle, once
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               name="foolsgold-backend")
