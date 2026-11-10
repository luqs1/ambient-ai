# -*- mode: python -*-
# The ambient-engine sidecar for Windows, as a folder (ambient-engine.exe + _internal\), not one file.
# Build: packaging\windows\build-windows.ps1   (or by hand:)
#   uv run --with pyinstaller pyinstaller --noconfirm --distpath build\engine-dist --workpath build\engine-work packaging\windows\ambient-engine.spec
#
# Why a folder: a one-file build unpacks ~300 MB into %TEMP% on every start, leaves it behind whenever the
# engine is killed, and would load the WinDivert kernel driver from that temp folder. As a folder the engine
# starts at once and the driver is loaded from the install directory (Program Files, admin-only).
import glob, os, sys
ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# engine/*.py also go in as files: mitmdump loads engine/addon.py from disk (-s), and the addon
# then imports the rest with `from engine import …` (from the archive when present, else these files).
modules = sorted(os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(ROOT, "engine", "*.py")))
datas = [(os.path.join(ROOT, "engine", m + ".py"), "engine") for m in modules]
datas += [(os.path.join(ROOT, "ui"), "ui")]          # index/panel/traffic.html + assets/ (css, js, fonts)
# The elevated capture helper and the WinDivert driver it loads: windows-redirector.exe, WinDivert.dll, WinDivert64.sys.
# (mitmproxy_rs ships a PyInstaller hook that collects these too; named here so a miss fails the check below.)
datas += collect_data_files("mitmproxy_windows")

hiddenimports = ["engine"] + ["engine." + m for m in modules if not m.startswith("__")]
hiddenimports += ["mitmproxy_windows"]
hiddenimports += collect_submodules("mitmproxy.addons")
hiddenimports += collect_submodules("mitmproxy.contentviews")
hiddenimports += collect_submodules("mitmproxy.proxy")
# Imported inside functions by engine.decide / discord / osal; named so a miss shows up as a build warning.
hiddenimports += ["numpy", "onnxruntime", "tokenizers", "zstandard", "psutil",
                  "huggingface_hub", "huggingface_hub.file_download"]

# The two local models (engine/decide.py), shipped as _internal\models\<repo>\<file> so the packaged app
# never downloads anything. Taken from the Hugging Face cache; fetched at build time if absent.
os.environ.pop("AMBIENT_MODELS_DIR", None)
sys.path.insert(0, ROOT)
from engine import decide
models = []
for repo in (decide.JUDGE_REPO, decide.EMBED_REPO):
    for f in decide.MODEL_FILES:
        src = os.path.realpath(decide._dl(repo, f))   # the cache holds links to blobs
        models.append((os.path.join("models", *repo.split("/"), *f.split("/")), src, "DATA"))
        print(f"model: {repo}/{f}  {os.path.getsize(src) / 1e6:.1f} MB")

a = Analysis(
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "mitmproxy.tools.web", "mitmproxy.tools.console",
              # huggingface_hub is only the dev fallback for finding a cached model file: leave out its
              # transfer backend, clients and CLI, and the test tooling fsspec drags in.
              "hf_xet", "fsspec", "pytest", "_pytest", "IPython",
              "huggingface_hub.inference", "huggingface_hub.cli", "huggingface_hub.hf_file_system"],
    noarchive=False,
)
a.datas += models
for need in ("windows-redirector.exe", "WinDivert.dll", "WinDivert64.sys"):
    if not any(os.path.basename(d[0]) == need for d in a.datas + a.binaries):
        raise SystemExit(f"{need} is missing from the bundle: local capture would not start")

pyz = PYZ(a.pure)
exe = EXE(
    # "X utf8": Python's UTF-8 mode, so files, logs and the text piped to and from agents are UTF-8 as on
    # macOS instead of the legacy Windows code page.
    pyz, a.scripts, [("X utf8", None, "OPTION")],
    exclude_binaries=True,
    name="ambient-engine",
    console=True,                 # the app starts it with CREATE_NO_WINDOW; run by hand it logs to the console
    upx=False,
    contents_directory="_internal",
    icon=os.path.join(ROOT, "app", "src-tauri", "icons", "icon.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="ambient-engine")
