# Ambient on Linux

Built and tested on Ubuntu 24.04.5 (kernel 6.8.0-147, XFCE on X11, x86_64) in the test VM described in
`docs/test-vms.md`, on 2026-11-10. Everything below was run there. What was not run is listed at the end.

## Build

```
packaging/linux/build-linux.sh            # -> dist/Ambient_0.1.0_amd64.deb (about 158 MB)
SKIP_ENGINE=1 packaging/linux/build-linux.sh   # reuse the last frozen engine
```

Needs uv, Rust, bun and Tauri v2's Linux build packages (`libwebkit2gtk-4.1-dev build-essential curl wget file
libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev`). Takes about 7 minutes in the VM from nothing, under 2 minutes when
`app/src-tauri/target` and `build/engine-work` from an earlier build are still there. Steps:

1. PyInstaller freezes the engine into one binary, `ambient-engine` (`packaging/linux/ambient-engine-linux.spec`).
   The two local models are inside it (`models/<repo>/<file>`, found through `AMBIENT_MODELS_DIR`), so it never
   downloads anything. PyInstaller is installed into the project's `.venv`, not run with `uv run --with`:
   mitmproxy_linux's PyInstaller hook looks for the capture helper in the environment's `bin` folder.
2. The capture helper is copied out of the `mitmproxy_linux` wheel (`mitmproxy-linux-redirector`, unmodified).
3. Tauri builds the shell and the `.deb`. The Linux bundle settings are written to `build/linux/tauri.linux.conf.json`
   and merged over `app/src-tauri/tauri.conf.json`, which stays as the Mac build wants it.

No AppImage: capture needs a root-owned helper with file capabilities at a fixed path, which an AppImage cannot
install. An AppImage would only work started from a terminal with sudo.

Run from source instead: `uv sync`, then `uv run python -m engine`. Without the package installed, mitmproxy
starts its helper with `sudo`, so use a terminal.

## What the package installs

| Path | What |
|---|---|
| `/usr/bin/ambient` | the tray app (Tauri) |
| `/usr/bin/ambient-engine` | the engine, started and stopped by the tray app |
| `/usr/lib/ambient/ambient-redirector` | mitmproxy's capture helper. `root:ambient-capture`, mode `0750`, capabilities `cap_net_admin,cap_bpf,cap_perfmon` |
| `/usr/lib/ambient/ambient-setup` | shell script, the only part that ever runs as root, and only when asked (below) |
| `/usr/share/polkit-1/actions/dev.ambient.desktop.policy` | the administrator prompt for `ambient-setup` |
| `/usr/share/applications/Ambient.desktop`, icons | menu entry |

Depends on `libcap2-bin` (setcap), `libnss3-tools` (certutil), `pkexec | policykit-1`, `ca-certificates`, `openssl`,
`procps`, plus what Tauri adds (`libwebkit2gtk-4.1-0`, `libgtk-3-0`, `libayatana-appindicator3-1`).

`postinst` creates the system group `ambient-capture`, applies owner, mode and capabilities to the helper, and adds
the person who ran `sudo apt install` (`$SUDO_USER`) to the group. It does not touch certificates. `prerm` stops a
running Ambient. `postrm purge` removes Ambient's certificates from the system list, its Firefox policy entries and
the group.

## Root, and why there is none at run time

mitmproxy's Linux capture is a helper that creates a tun device and attaches an eBPF program to the root cgroup;
the program binds each new socket of a matching process to the tun device, and the helper passes the packets to the
engine over a unix socket. mitmproxy 12 (`mitmproxy_rs` 0.12.15) starts the helper with exactly two commands,
`sudo echo -n` and `sudo --non-interactive --preserve-env <helper> <socket folder>`, both hard-coded. A desktop app
has no terminal for sudo to ask in, and when the helper cannot start mitmproxy exits.

What Ambient does instead:

- The helper needs three capabilities, not root. Measured: with `cap_net_admin,cap_bpf` it creates the tun device
  and then fails loading the program; adding `cap_perfmon` makes it work; `cap_sys_resource` is not needed. The
  package sets those three as file capabilities, the way Wireshark does for `dumpcap`, and limits who may run it
  to the group `ambient-capture`. The helper runs as the user. Nothing of Ambient runs as root while it captures.
- `engine/osal.py` (`_linux_prepare_capture`) tells mitmproxy to use the installed helper and, for the seconds
  start-up takes, puts a stand-in `sudo` first on the engine's own `PATH`. It answers the two commands above and
  hands anything else to the real sudo. The stand-in is a workaround for the hard-coded `sudo`, nothing more: the
  permission check is the helper's group and capabilities.
- If the account was added to the group after logging in, the helper is started through `sg ambient-capture`, so
  no log-out is needed. (`sg` is the system's own set-uid tool; it stays as the helper's parent, shown as root in
  `ps`. After the next login the helper is started directly.)
- mitmproxy never tells the helper to stop. The engine does it on exit (SIGTERM; the helper removes its tun device
  and the eBPF program goes with it), and at start it stops a helper left behind by an engine that was killed.
- If capture cannot start (not in the group, old kernel), the engine comes up without it, so the window can say
  what is missing, instead of not coming up at all.

Being in `ambient-capture` means: this account may redirect the network traffic of any process on this computer
through a program it controls. That is what Ambient does, and it is also what the group grants to anything else
running as that user.

## The certificate

The engine creates its certificate authority on first run in `~/.mitmproxy/`. Nothing trusts it until the person
clicks **Trust** in Ambient's window. That runs `osal.trust_ca()`:

1. `pkexec /usr/lib/ambient/ambient-setup trust` (one administrator prompt; `sudo -n` is tried first and only
   works where sudo needs no password). The script takes no arguments from the caller. For the user who asked
   it copies `~/.mitmproxy/mitmproxy-ca-cert.pem` to a root-owned temp file, checks it is one certificate with
   `CA:TRUE`, installs it as `/usr/local/share/ca-certificates/ambient-<user>.crt` and runs
   `update-ca-certificates` (Fedora, Arch and openSUSE locations are in the script but were not run). curl, wget,
   git, Go and Python's `ssl` read that list.
2. Firefox does not read the system list on Linux, and `ImportEnterpriseRoots` is a Windows and macOS policy. So
   the same script writes `/etc/firefox/policies/policies.json` with `Certificates.Install` pointing at a copy of
   the certificate in `/etc/firefox/policies/` (merged into an existing file with python3, never overwriting other
   policies). The copy sits there because the snap build can read `/etc/firefox` but not `/usr/local`.
3. It adds the user to `ambient-capture`.
4. Then, without root, `certutil` adds the certificate to every browser database of this user: `~/.pki/nssdb` and
   `~/.local/share/pki/nssdb` (Chrome, Chromium, Edge, Brave), the same pair inside `~/snap/chromium/current/`
   and Flatpak homes, and each Firefox-family profile (`~/.mozilla/firefox/*`, `~/snap/firefox/common/…`,
   Flatpak, LibreWolf, Zen, Thunderbird). This step is repeated at every engine start once the system list has
   the certificate, so a browser installed later, or a new profile, is covered after Ambient restarts.

A browser that is open must be restarted to notice. When trust succeeds the engine forgets which apps refused the
certificate before, so they are read from the next connection rather than a day later.

If the person closes the administrator prompt, nothing is changed, the browser databases included (checked on the
final package). The window then opens the certificate file in the desktop's certificate viewer: that is the UI's
fallback on every OS when trusting fails, and after a deliberate Cancel it is more than was asked for. Where there
is no polkit at all, only step 4 runs.

The private key of this certificate is in `~/.mitmproxy/`. Trusting it system-wide means whoever can read that
folder can impersonate any site to any account on this computer.

`osal.ca_trusted()` is true when exactly this certificate is in the system bundle.

## What gets captured

Two facts about mitmproxy's helper decide this. Both were checked in the VM.

- A pattern is a process id or the exact name of the *thread* that creates the socket (15 characters), and there
  is room for 19. `local:firefox` captured nothing from Firefox (its sockets are made by a thread called
  `Socket Thread`); `local:chrome` captured nothing from Chrome (`Chrome_ChildIOT`).
- A socket bound to the tun device cannot be reached by anyone once it listens. With everything captured,
  `python3 -m http.server 9999` started afterwards showed as `0.0.0.0%tun0:9999` and refused every connection,
  from the same machine and from outside. The kernel does not let an unprivileged process undo the binding, and
  an eBPF hook cannot either (`sock_bindtoindex_locked` wants `CAP_NET_RAW`).

So the default on Linux is not "every app". It is the networking threads of browsers and of everything built on
them, plus a few command-line tools, none of which listen:

```
Socket Thread, Chrome_ChildIOT, curl, wget, git-remote-http, gh, claude, codex
```

`Socket Thread` is Firefox, Zen, LibreWolf and Thunderbird. `Chrome_ChildIOT` is Chrome, Chromium, Brave, Edge and
Electron apps. Verified: Firefox (snap), Google Chrome (deb), Chromium (snap), curl. `wget` was verified in "all"
mode only; `gh`, `claude`, `codex` and Electron apps were not run.

To change it, set `AMBIENT_CAPTURE` or put one line in `~/.config/ambient/capture`:

- names separated by commas: added to the list above (thread or process names; 18 in total)
- `all`: every process except VPN daemons, `systemd-resolved`, `systemd-networkd`, NetworkManager, avahi, sshd,
  Docker and call apps. Verified with curl, wget and Python. **Any server started while Ambient is watching is
  unreachable until it is restarted with Ambient paused**: dev servers, container ports, anything that listens.
  `systemd-resolved` has to be excluded: with it captured, every DNS lookup on the machine failed
  (`IP_UNICAST_IF` returns `EINVAL` on a socket already bound to another device).

This default is a decision for the owner of the product to confirm: it trades "any app" for not breaking servers.

Which app a connection belongs to: the Linux helper sends no process information (`tunnel_info: None`), so the
engine looks up who owns the source port (`lsof`, about 15 ms) and names the executable (`osal.app_name`). It can
only see this user's processes; in "all" mode another user's or root's traffic shows as `unknown`.

## What a person has to do

1. `sudo apt install ./Ambient_0.1.0_amd64.deb`
2. Open Ambient from the applications menu. A tray icon appears; its menu has Show what's happening, Open Ambient,
   Pause watching and Quit Ambient.
3. Click **Trust** in the window and give an administrator password once.
4. Restart browsers that were open.

Installed from a graphical software centre instead, nobody is added to the group at install; step 3 does it, and
the window then asks for Pause watching, Resume watching once.

Log: `~/.local/state/ambient/engine.log`. Data: `~/.local/share/ambient/`.

## Known limitations

- **Kernel 6.8 or newer** (mitmproxy's requirement). Older kernels: Ambient starts without capture and says so.
  Not run on an older kernel.
- **IPv6 that is advertised but does not work breaks Chrome under capture.** The VM's network (QEMU user mode)
  hands out an IPv6 address and route that refuse every connection. Without Ambient, apps fall back to IPv4 at
  once. With it, the proxy accepts the app's IPv6 connection before it knows the server is unreachable, then
  closes it, and Chrome reports `ERR_CONNECTION_CLOSED` instead of trying IPv4 (Firefox and curl recovered). To
  go on testing, IPv6 was switched off in the VM (`/etc/sysctl.d/99-ambient-test-no-ipv6.conf`). Working IPv6 was
  therefore not tested at all. A fix belongs in the engine (for example dropping AAAA answers when IPv6 is
  unreachable).
- **Pages served from the browser's cache make no "open a page" event** (a 304 has no title). Seen with a
  second visit to example.com.
- **Left click on the tray icon opens the menu**, not the panel: Linux tray icons report no clicks. The panel is
  the first menu item and opens at the top right, since the icon's position is not known either.
- **Wayland was not tested.** The VM runs X11. Panel placement and always-on-top are the parts most likely to
  differ. GNOME needs the AppIndicator extension to show any tray icon.
- **Snap and Flatpak browsers.** Snap Firefox and snap Chromium work as described above. Flatpak paths are in the
  code but no Flatpak browser was installed. Whether a Flatpak Firefox can read `/etc/firefox/policies` was not
  checked; its profiles get the certificate directly in any case.
- **The administrator prompt was shown and cancelled, not completed.** See "Not verified" below.
- **One crash at exit was seen**, once: the source engine was interrupted two seconds after starting and a thread
  inside onnxruntime segfaulted while the process was already going down (`dmesg`: segfault in
  `onnxruntime_pybind11_state`). Capture was released normally. It did not happen in any other stop.
- **The UI's own notification-permission call is refused** (`plugin:notification|is_permission_granted not allowed
  by ACL`, logged once per window load). Notifications still appear, because the shell posts them. This is the
  shared capability file, the same on every OS.
- **Calls** (WebRTC) in a captured browser can break, as on macOS. Not tested here.
- **If the engine is killed outright** (SIGKILL), the helper stays until the next connection of a captured app,
  which fails once; then the helper exits by itself. The next start of Ambient also removes it.
- **arm64** was not built.
- At Quit the engine log ends with `Task failed: failed to send packet … Connection refused`: the helper noticing
  the engine's socket is gone while both are stopping. Noise; the helper, tun device and eBPF program are gone
  afterwards.
- "Show me once" also lists the browser's own background requests if one happens while it watches (Firefox's
  `mozilla-ohttp.fastly-edge.com` showed up as "Request · Fastly-edge"), and labels come from API names: a
  search on hn.algolia.com is "Item dev · Algolia".
- The engine log gets a block of eBPF type information at every start (the helper's own warning when the kernel
  rejects its BTF; it then loads without). Noise, not an error.

## Verified in the VM

All with the installed package (`sudo apt install ./Ambient_0.1.0_amd64.deb`), started from the XFCE application
finder, unless it says "from source". Screenshots are in `docs/screenshots/linux-*.png`.

| What | Result |
|---|---|
| Engine from source, proxy mode | `/api/setup`, `/api/activity`, `/api/triggers` answer |
| Install | helper `root:ambient-capture 0750` with `cap_net_admin,cap_perfmon,cap_bpf=ep`; installing user in the group |
| Start | `ambient` → `ambient-engine` → `ambient-redirector` running as the user; tun device up; one `cgroup_inet_sock_create` program attached |
| Tray | icon in the XFCE panel; all four menu items work (`linux-01-tray.png`) |
| Window, panel | `linux-02-main-window.png`, `linux-05-panel.png` |
| Trust button | system bundle, Firefox policy, four browser databases; `curl https://example.com` verifies through the proxy |
| Capture | Firefox (snap), Google Chrome (deb), Chromium (snap), curl: decrypted, named correctly, "open a page" kinds for Example, Wikipedia, Hacker News, GitHub (`linux-03-activity.png`) |
| App icons | `/api/icon/` returns PNGs for Firefox, Google Chrome, Chromium (from their `.desktop` entries); none for curl |
| Trigger, exact | "I open a page on example.com" fired on a Firefox visit; stand-in agent result in `/api/moments` (`linux-04-trigger-fired.png`) |
| Trigger, semantic | grocery note matched at 0.994, tax note rejected at 0.0, by the bundled model, with `~/.cache/huggingface` moved away and not recreated |
| Notification | `linux-06-notification.png` |
| A server started while watching | `python3 -m http.server 9999` listened on `0.0.0.0:9999` and answered (default list). In "all" mode it did not, as described above |
| Pause, Resume, Quit | after Pause and after Quit: no engine, no helper, no tun device, no eBPF program, `curl` works directly |
| Account not in the group (from source, sudo made to fail) | engine comes up without capture and says why; after `ambient-setup allow` it asks for Pause/Resume; after a restart capture runs through `sg` |
| Purge | `apt purge ambient` removed the certificate from the system bundle, the Firefox policy and the group |
| "all" mode (from source) | DNS works; curl, wget and Python captured and named |

Checked again on a package rebuilt from commit `1302917` (API hardening, "any website" group), installed over
the earlier one and started on the desktop with a stand-in agent
(`AMBIENT_AGENT_CMD="python3 …/scripts/standin_agent.py" ambient`; the tray app hands its environment to the
engine). No coding agent is installed in the VM.

| What | Result |
|---|---|
| Header | "Listening" with capture on and the certificate trusted. Started with an empty `HOME` (new, untrusted certificate): "Needs setup" and the Trust step |
| Local API | `Host: evil.example` → 403 ("this address is not Ambient's"); foreign `Origin` → 403; POST without `X-Ambient: 1` → 403; plain request → 200. Listens on 127.0.0.1 only |
| Database | `~/.local/share/ambient` is `0700`, `ambient.db`, `-wal` and `-shm` are `0600`; the earlier build's `0644` files were tightened at first start |
| A real site, not signed in | Firefox (snap): GitHub pull request, issues list and repository pages, python.org, rust-lang.org. Activity shows GitHub "open a page" with the page titles (`linux-08-real-site-activity.png`) |
| Trigger from a sentence | "I open a pull request" → suggestion ticks "any website" (`*\|VISIT`), content check on. Saved as automatic: fired on `mitmproxy/mitmproxy/pull/7000` (0.955), not on the repository page (0.044) or rust-lang.org (0.001) (`linux-11-trigger-fired-on-pull-request.png`) |
| Builder in the window | N opens it; typing the sentence shows the same suggestion and the backtest: 96% and 97% for the two pull requests, 8%, 4%, 0%, 0% for the rest (`linux-09-builder-any-website.png`) |
| Show me once | pressed in the builder, then a search typed on hn.algolia.com in Firefox: listed as "seen just now" with the search text (`linux-10-show-me-once.png`) |
| Quit from the tray | no `ambient`, engine or helper process, no tun device, no eBPF program; `curl` to example.com and github.com answers 200 |

Current package: rebuilt from commit `1927856`, `Ambient_0.1.0_amd64.deb`, 164,894,708 bytes, SHA-256
`d0341f68014b75b1f7453d7792e3cf3539254b0055752b616d1786068ae9e78b`. Installed over the one above and started with
`gtk-launch Ambient` in the desktop session's environment: `/api/setup` 200, `Host: evil.example` 403, capture on;
after quitting (`pkill -INT ambient`) no processes, tun device or eBPF program, `curl` works. Agent CLIs in
`~/.local/bin` are now found from a launcher: a stub `claude` there showed `available: true`, also when the app was
given a bare `PATH=/usr/local/bin:/usr/bin:/bin` (stub removed afterwards).

## Not verified

- **Typing a password into the administrator prompt.** The prompt appears with Ambient's message when the app
  is started from the desktop and sudo is unavailable (`linux-07-admin-prompt.png`), and cancelling it leaves
  everything unchanged. Authenticating was not done. In the VM the Trust button went through passwordless
  `sudo -n` instead, and the helper was also run under a pkexec-like environment
  (`sudo env -i PKEXEC_UID=1000 /usr/lib/ambient/ambient-setup trust`), which worked.
- An install from a graphical software centre (no `$SUDO_USER`).
- Fedora, Arch, openSUSE certificate locations; any distribution other than Ubuntu 24.04.
- Wayland, GNOME, KDE. Working IPv6. Flatpak browsers. Electron apps. `gh`, `claude`, `codex`. arm64.
- Veil (redaction for agent CLIs) on Linux.
- "Protect your coding agents" (writes `~/.bashrc`): not clicked.
