"""An app opened from Finder or a desktop launcher gets the system's bare PATH. Agents installed in the
user's own folders (~/.local/bin for Claude Code, Homebrew for Codex) must still be found."""
import os
import stat
import sys

import pytest

from engine import osal

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="Windows apps get the user's PATH")


def _tool(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text("#!/bin/sh\necho hi\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def test_agents_in_user_folders_are_found_from_a_bare_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    tool = _tool(home / ".local" / "bin", "claude")
    monkeypatch.setattr(osal, "HOME", home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr(osal, "login_shell_path", lambda timeout=5.0: [])
    osal.adopt_user_path()
    import shutil
    assert shutil.which("claude") == str(tool)
    assert os.environ["PATH"].startswith("/usr/bin:/bin:/usr/sbin:/sbin")      # kept, in order


def test_the_login_shells_path_is_added_and_nothing_is_lost(tmp_path, monkeypatch):
    fnm = tmp_path / "fnm" / "bin"
    _tool(fnm, "node")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(osal, "login_shell_path", lambda timeout=5.0: ["/usr/bin", str(fnm), "/does/not/exist"])
    path = osal.adopt_user_path().split(os.pathsep)
    assert path[:2] == ["/usr/bin", "/bin"] and str(fnm) in path
    assert "/does/not/exist" not in path and path.count("/usr/bin") == 1


def test_asking_the_shell_reads_only_the_marked_line(monkeypatch, tmp_path):
    shell = tmp_path / "fakesh"
    shell.write_text("#!/bin/sh\necho 'welcome banner'\nprintf '\\n__AMBIENT_PATH__/a:/b\\n'\necho 'more noise'\n")
    shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(shell))
    assert osal.login_shell_path() == ["/a", "/b"]


def test_a_broken_shell_is_harmless(monkeypatch):
    monkeypatch.setenv("SHELL", "/no/such/shell")
    assert osal.login_shell_path() == []
