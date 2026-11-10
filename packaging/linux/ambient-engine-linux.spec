# -*- mode: python -*-
# The Linux engine sidecar: packaging/ambient-engine.spec adapted for Linux (see packaging/linux/build-linux.sh).
# Build: packaging/linux/build-linux.sh (PyInstaller must run from the project's .venv, see there)
import glob, os, sys
ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

from PyInstaller.utils.hooks import collect_submodules

# engine/*.py also go in as files: mitmdump loads engine/addon.py from disk (-s), and the addon
# then imports the rest with `from engine import …` (from the archive when present, else these files).
modules = sorted(os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(ROOT, "engine", "*.py")))
datas = [(os.path.join(ROOT, "engine", m + ".py"), "engine") for m in modules]
datas += [(os.path.join(ROOT, "ui"), "ui")]          # index/panel/traffic.html + assets/ (css, js, fonts)

hiddenimports = ["engine"] + ["engine." + m for m in modules if not m.startswith("__")]
# mitmproxy_linux only tells mitmproxy where the capture helper is. The helper itself is NOT put inside this
# binary: the package installs it at /usr/lib/ambient/ambient-redirector with file capabilities, and a copy
# unpacked to a temp folder on every start could carry none.
hiddenimports += ["mitmproxy_linux"]
hiddenimports += collect_submodules("mitmproxy.addons")
hiddenimports += collect_submodules("mitmproxy.contentviews")
hiddenimports += collect_submodules("mitmproxy.proxy")
# Imported inside functions by engine.decide / discord / osal; named so a miss shows up as a build warning.
hiddenimports += ["numpy", "onnxruntime", "tokenizers", "zstandard", "psutil",
                  "huggingface_hub", "huggingface_hub.file_download"]

# The two local models (engine/decide.py), shipped inside the binary as models/<repo>/<file> so the
# packaged app never downloads anything (engine/__main__.py points AMBIENT_MODELS_DIR at them when frozen).
# Taken from the Hugging Face cache; fetched at build time if absent.
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
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "mitmproxy.tools.web", "mitmproxy.tools.console",
              "hf_xet", "fsspec", "pytest", "_pytest", "IPython",
              "huggingface_hub.inference", "huggingface_hub.cli", "huggingface_hub.hf_file_system"],
    noarchive=False,
)
a.binaries = [b for b in a.binaries if os.path.basename(b[0]) != "mitmproxy-linux-redirector"]
a.datas = [d for d in a.datas if os.path.basename(d[0]) != "mitmproxy-linux-redirector"]
a.datas += models
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="ambient-engine",
    console=True,
    upx=False,
    strip=False,
)
