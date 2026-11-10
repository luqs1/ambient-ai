"""OS abstraction: the pure, path-based helpers, for each OS (SYSTEM is monkeypatched; nothing is executed)."""
import pytest

from engine import osal
from engine.events import BROWSERS, PROC_NAMES
from engine.localstore import SKIP_APPS

MAC = [
    ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "Google Chrome"),
    ("/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Versions/131.0.6778.86/Helpers/"
     "Google Chrome Helper.app/Contents/MacOS/Google Chrome Helper", "Google Chrome"),               # outermost bundle wins
    ("/Applications/Slack.app/Contents/Frameworks/Slack Helper.app/Contents/MacOS/Slack Helper", "Slack"),
    ("/Applications/Zen.app/Contents/MacOS/zen", "Zen"),
    ("/Users/sam/Applications/Notion.app/Contents/MacOS/Notion", "Notion"),
    ("/System/Applications/Mail.app/Contents/MacOS/Mail", "Mail"),
    ("/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/com.apple.WebKit.Networking.xpc/Contents/MacOS/"
     "com.apple.WebKit.Networking", "com.apple.WebKit.Networking"),
    ("/opt/homebrew/Cellar/git/2.47.0/libexec/git-core/git-remote-https", "git-remote-https"),
    ("/usr/bin/curl", "curl"), ("node", "node"), ("", "unknown"),
]
WIN = [
    (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "Google Chrome"),
    (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "Microsoft Edge"),
    (r"C:\Users\sam\AppData\Local\slack\app-4.41.105\Slack.exe", "Slack"),
    (r"C:\Users\sam\AppData\Local\Programs\Microsoft VS Code\Code.exe", "Visual Studio Code"),
    (r"C:\Program Files\WindowsApps\5319275A.WhatsAppDesktop_2.2445.7.0_x64__cv1g1gvanyjgm\WhatsApp.exe", "WhatsApp"),
    (r"C:\Program Files\Mozilla Firefox\FIREFOX.EXE", "Firefox"),
    (r"C:\Users\sam\AppData\Local\Programs\Opera\opera.exe", "Opera"),
    (r"C:\Users\sam\AppData\Local\Vivaldi\Application\vivaldi.exe", "Vivaldi"),
    (r"C:\Program Files\LibreWolf\librewolf.exe", "LibreWolf"),
    (r"C:\Tools\Some Tool\SomeTool.exe", "SomeTool"), ("C:/msys64/usr/bin/curl.exe", "curl"), ("", "unknown"),
]
LINUX = [
    ("/opt/google/chrome/chrome", "Google Chrome"), ("/usr/lib/firefox/firefox", "Firefox"), ("/usr/lib/firefox/firefox-bin", "Firefox"),
    ("/usr/lib/chromium/chromium", "Chromium"), ("/snap/slack/current/usr/lib/slack/slack", "Slack"),
    ("/usr/share/code/code", "Visual Studio Code"), ("/opt/zen/zen-bin", "Zen"), ("/opt/brave.com/brave/brave", "Brave"),
    ("/snap/chromium/3583/usr/lib/chromium-browser/chrome", "Chromium"), ("/usr/lib/firefox-esr/firefox-esr", "Firefox"),
    ("/usr/bin/curl", "curl"), ("/usr/bin/Telegram", "Telegram"), ("", "unknown"),
]


@pytest.mark.parametrize("system, path, want", [("darwin", p, w) for p, w in MAC] + [("windows", p, w) for p, w in WIN]
                         + [("linux", p, w) for p, w in LINUX])
def test_app_name(monkeypatch, system, path, want):
    monkeypatch.setattr(osal, "SYSTEM", system)
    assert osal.app_name(path) == want


def test_app_names_line_up_with_the_event_layer(monkeypatch):
    """What osal calls a browser must be what events.py treats as one, or its tabs are not split by site."""
    for system, cases in (("darwin", MAC), ("windows", WIN), ("linux", LINUX)):
        monkeypatch.setattr(osal, "SYSTEM", system)
        for path, _ in cases:
            name = osal.app_name(path)
            if any(b in path.lower() for b in ("chrome", "firefox", "msedge", "zen", "brave", "chromium", "webkit", "opera",
                                               "vivaldi", "librewolf")):
                assert name in BROWSERS, (system, path, name)
    monkeypatch.setattr(osal, "SYSTEM", "darwin")
    assert PROC_NAMES[osal.app_name("/opt/homebrew/Cellar/git/2.47.0/libexec/git-core/git-remote-https")] == "git"


@pytest.mark.parametrize("system, path", [
    ("linux", "/usr/lib/firefox-esr/firefox-esr"), ("linux", "/usr/lib/chromium-browser/chromium-browser"),
    ("linux", "/usr/bin/librewolf"), ("linux", "/opt/vivaldi/vivaldi-bin"), ("linux", "/usr/lib/x86_64-linux-gnu/opera/opera"),
    ("linux", "/snap/chromium/3583/usr/lib/chromium-browser/chrome"), ("linux", "/opt/microsoft/msedge/msedge"),
    ("windows", r"C:\Users\sam\AppData\Local\Programs\Opera\opera.exe"), ("windows", r"C:\Program Files\LibreWolf\librewolf.exe"),
])
def test_other_browsers_are_recognised_as_browsers(monkeypatch, system, path):
    monkeypatch.setattr(osal, "SYSTEM", system)
    name = osal.app_name(path)
    assert name in BROWSERS and name in SKIP_APPS


@pytest.mark.parametrize("system, path, want", [
    ("darwin", "/Applications/Slack.app/Contents/MacOS/Slack", True),
    ("darwin", "/Users/sam/Applications/Notion.app/Contents/MacOS/Notion", True),
    ("darwin", "/System/Applications/Mail.app/Contents/MacOS/Mail", False),
    ("darwin", "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder", False),
    ("darwin", "/usr/sbin/mDNSResponder", False), ("darwin", "/usr/libexec/trustd", False),
    ("darwin", "/Library/Apple/System/Library/CoreServices/XProtect.app/Contents/MacOS/XProtect", False),
    ("darwin", "/opt/homebrew/bin/node", False), ("darwin", "", False),
    ("windows", r"C:\Users\sam\AppData\Local\slack\slack.exe", True),
    ("windows", r"C:\Program Files\WindowsApps\5319275A.WhatsAppDesktop_2.2445.7.0_x64__cv1g1gvanyjgm\WhatsApp.exe", True),
    ("windows", r"C:\Windows\System32\svchost.exe", False), ("windows", r"c:\WINDOWS\explorer.exe", False), ("windows", "", False),
    ("linux", "/usr/bin/firefox", True), ("linux", "/snap/slack/current/usr/lib/slack/slack", True),
    ("linux", "/usr/lib/systemd/systemd-resolved", False), ("linux", "/usr/sbin/NetworkManager", False),
    ("linux", "/usr/libexec/packagekitd", False), ("linux", "/sbin/dhclient", False), ("linux", "", False),
])
def test_is_user_app(monkeypatch, system, path, want):
    monkeypatch.setattr(osal, "SYSTEM", system)
    assert osal.is_user_app(path) is want


def test_app_data_roots_mac(monkeypatch, tmp_path):
    monkeypatch.setattr(osal, "SYSTEM", "darwin")
    monkeypatch.setattr(osal, "HOME", tmp_path)
    lib = tmp_path / "Library"
    for d in ("Application Support/WhatsApp", "Group Containers/group.net.whatsapp.WhatsApp.shared", "Containers/net.whatsapp.WhatsApp",
              "Application Support/Other", "Group Containers/group.com.example.other"):
        (lib / d).mkdir(parents=True)
    got = {str(p.relative_to(lib)) for p in osal.app_data_roots("WhatsApp")}
    assert got == {"Application Support/WhatsApp", "Group Containers/group.net.whatsapp.WhatsApp.shared", "Containers/net.whatsapp.WhatsApp"}
    assert osal.app_data_roots("Nothing Here") == []


def test_app_data_roots_windows_and_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(osal, "HOME", tmp_path)
    monkeypatch.setattr(osal, "SYSTEM", "windows")
    roaming, local = tmp_path / "AppData" / "Roaming", tmp_path / "AppData" / "Local"
    (roaming / "Jotter").mkdir(parents=True)
    (local / "Packages" / "1234Vendor.Jotter_abc" / "LocalState").mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert set(osal.app_data_roots("Jotter")) == {roaming / "Jotter", local / "Packages" / "1234Vendor.Jotter_abc" / "LocalState"}

    monkeypatch.setattr(osal, "SYSTEM", "linux")
    (tmp_path / ".config" / "jotter").mkdir(parents=True)
    (tmp_path / "snap" / "jotter").mkdir(parents=True)
    got = {str(p.relative_to(tmp_path)).lower() for p in osal.app_data_roots("Jotter")}       # (macOS test hosts fold case)
    assert got == {".config/jotter", "snap/jotter"}


def test_app_data_roots_linux_stays_inside_the_app(monkeypatch, tmp_path):
    monkeypatch.setattr(osal, "HOME", tmp_path)
    monkeypatch.setattr(osal, "SYSTEM", "linux")
    (tmp_path / ".var" / "app" / "org.other.Wallet" / "data").mkdir(parents=True)
    (tmp_path / ".var" / "app" / "io.example.Jotter" / "data").mkdir(parents=True)
    (tmp_path / ".config" / "jotter").mkdir(parents=True)
    roots = osal.app_data_roots("Jotter")
    assert tmp_path / ".var" / "app" not in roots                    # never every Flatpak app's data
    assert tmp_path / ".var" / "app" / "io.example.Jotter" in roots and not any("Wallet" in str(r) for r in roots)


def test_support_and_cache_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(osal, "HOME", tmp_path)
    for k in ("LOCALAPPDATA", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(k, raising=False)
    want = {"darwin": ("Library/Application Support/Ambient", "Library/Caches/Ambient"),
            "windows": ("AppData/Local/Ambient", "AppData/Local/Ambient/cache"),
            "linux": (".local/share/ambient", ".cache/ambient")}
    for system, (support, cache) in want.items():
        monkeypatch.setattr(osal, "SYSTEM", system)
        assert osal.support_dir() == tmp_path / support and osal.support_dir().is_dir()
        assert osal.cache_dir() == tmp_path / cache and osal.cache_dir().is_dir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert osal.support_dir() == tmp_path / "xdg" / "ambient"
    assert osal.ca_path() == tmp_path / ".mitmproxy" / "mitmproxy-ca-cert.pem"


@pytest.mark.parametrize("system", ["darwin", "windows", "linux"])
def test_per_os_tables_are_complete(monkeypatch, system):
    monkeypatch.setattr(osal, "SYSTEM", system)
    ex = osal.default_excludes().split(",")
    assert len(ex) >= 5 and all(x.startswith("!") and len(x) > 1 for x in ex)
    assert any("tailscale" in x.lower() for x in ex) and any("zoom" in x.lower() for x in ex)
    assert osal.shell_profile_target() and osal.trust_ca_hint()


def test_bare_name_and_split_cmd(monkeypatch):
    monkeypatch.setattr(osal, "SYSTEM", "darwin")
    assert osal.bare_name("/opt/homebrew/bin/claude") == "claude"
    assert osal.split_cmd('claude -p "two words" --flag') == ["claude", "-p", "two words", "--flag"]
    monkeypatch.setattr(osal, "SYSTEM", "windows")
    assert osal.bare_name(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe") == "Microsoft Edge" and osal.bare_name("") == ""
    assert osal.split_cmd(r'C:\Tools\agent.exe -p "two words"') == [r"C:\Tools\agent.exe", "-p", "two words"]


# ---------- which processes capture starts with ----------
@pytest.fixture
def linux_capture(monkeypatch, tmp_path):
    """Linux with the 'can capture start?' probe stubbed (the real one may run sudo -n)."""
    monkeypatch.setattr(osal, "SYSTEM", "linux")
    monkeypatch.setattr(osal, "HOME", tmp_path)
    monkeypatch.setattr(osal, "_linux_state", {"wanted": 0.0, "sudo": (0.0, False)})
    monkeypatch.delenv("AMBIENT_CAPTURE", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    answer = {"v": ("ready", "")}
    monkeypatch.setattr(osal, "_linux_capture_possible", lambda: answer["v"])
    return answer


def test_capture_spec_linux_defaults_to_a_bounded_app_list(linux_capture):
    names = osal.capture_spec().split(",")
    assert names == osal._LINUX_APPS[:osal._LINUX_MAX_PATTERNS] and 1 <= len(names) <= osal._LINUX_MAX_PATTERNS
    assert not any(n.startswith("!") for n in names)


def test_capture_spec_linux_choice_from_env_and_file(linux_capture, monkeypatch, tmp_path):
    monkeypatch.setenv("AMBIENT_CAPTURE", "all")
    assert osal.capture_spec() == osal.default_excludes() and osal.capture_spec().startswith("!")
    monkeypatch.setenv("AMBIENT_CAPTURE", "apps, jotter ,jotter")
    spec = osal.capture_spec().split(",")
    assert len(spec) <= osal._LINUX_MAX_PATTERNS and "apps" not in spec and spec.count("jotter") <= 1
    assert "jotter" in spec or len(osal._LINUX_APPS) >= osal._LINUX_MAX_PATTERNS
    monkeypatch.delenv("AMBIENT_CAPTURE")
    cfg = tmp_path / ".config" / "ambient"
    cfg.mkdir(parents=True)
    (cfg / osal.LINUX_CAPTURE_FILE).write_text("# what to watch\n\nall\n")
    assert osal.capture_spec() == osal.default_excludes()


def test_capture_spec_is_none_when_capture_cannot_start(linux_capture):
    linux_capture["v"] = ("missing", "Seeing app traffic needs root on Linux.")
    assert osal.capture_spec() is None


@pytest.mark.parametrize("system", ["darwin", "windows"])
def test_capture_spec_and_prepare_off_linux(monkeypatch, system):
    monkeypatch.setattr(osal, "SYSTEM", system)
    def never():
        raise AssertionError("Linux-only code ran")
    monkeypatch.setattr(osal, "_linux_prepare_capture", never)
    monkeypatch.setattr(osal, "_linux_capture_spec", never)
    assert osal.prepare_capture() is None
    assert osal.capture_spec() == osal.default_excludes()
