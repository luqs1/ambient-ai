# -*- mode: python -*-
# Build: uv run --with pyinstaller pyinstaller --noconfirm --distpath build/engine-dist --workpath build/engine-work packaging/ambient-engine.spec
import glob, os, sys
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

from PyInstaller.utils.hooks import collect_submodules

# engine/*.py also go in as files: mitmdump loads engine/addon.py from disk (-s), and the addon
# then imports the rest with `from engine import …` (from the archive when present, else these files).
modules = sorted(os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(ROOT, "engine", "*.py")))
datas = [(os.path.join(ROOT, "engine", m + ".py"), "engine") for m in modules]
datas += [(os.path.join(ROOT, "ui"), "ui")]          # index/panel/traffic.html + assets/ (css, js, fonts)

hiddenimports = ["engine"] + ["engine." + m for m in modules if not m.startswith("__")]
hiddenimports += ["mitmproxy_macos"]
hiddenimports += collect_submodules("mitmproxy.addons")
hiddenimports += collect_submodules("mitmproxy.contentviews")
hiddenimports += collect_submodules("mitmproxy.proxy")
# Imported inside functions by engine.decide / discord / osal; named so a miss shows up as a build warning.
hiddenimports += ["numpy", "onnxruntime", "tokenizers", "zstandard", "psutil",
                  "huggingface_hub", "huggingface_hub.file_download"]

# The two local models (engine/decide.py), shipped inside the binary as models/<repo>/<file> so the
# packaged app never downloads anything. Taken from the Hugging Face cache; fetched at build time if absent.
os.environ.pop("AMBIENT_MODELS_DIR", None)
sys.path.insert(0, ROOT)
from engine import decide
models = []
for repo in (decide.JUDGE_REPO, decide.EMBED_REPO):
    for f in decide.MODEL_FILES:
        src = os.path.realpath(decide._dl(repo, f))   # the cache holds symlinks to blobs
        models.append((os.path.join("models", *repo.split("/"), *f.split("/")), src, "DATA"))
        print(f"model: {repo}/{f}  {os.path.getsize(src) / 1e6:.1f} MB")

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
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
# The Python binding (onnxruntime_pybind11_state.so) is self-contained; the wheel's C-API dylib is unused.
a.binaries = [b for b in a.binaries if not os.path.basename(b[0]).startswith("libonnxruntime")]
a.datas += models
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="ambient-engine",
    console=True,
    upx=False,
    target_arch="arm64",
    codesign_identity=None,
)
