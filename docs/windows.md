# Ambient on Windows

Built and tested on Windows 11 Enterprise (evaluation, build 26300, x64) in the test VM described in
`docs/test-vms.md`, on 2026-11-10. Everything below was run there. What was not run is listed at the end.

## Build

```
powershell -ExecutionPolicy Bypass -File packaging\windows\build-windows.ps1
... -SkipEngine         # reuse the last frozen engine
... -Bundles nsis       # only the .exe installer (default: nsis,msi)
```

Output in `dist\`: `Ambient_0.1.0_x64-setup.exe` (NSIS, 123 MB) and `Ambient_0.1.0_x64_en-US.msi` (141 MB).
Needs uv, Rust (MSVC toolchain, Visual Studio Build Tools with "Desktop development with C++") and bun on
`PATH`. No administrator rights. A first build takes about 15 minutes in the VM. Tauri downloads NSIS and WiX
by itself. Steps:

1. PyInstaller freezes the engine as a **folder**: `ambient-engine.exe` plus `_internal\` (279 MB, 227 files),
   from `packaging\windows\ambient-engine.spec`. The two local models are inside
   (`_internal\models\<repo>\<file>`, found through `AMBIENT_MODELS_DIR`), so it never downloads anything.
   A one-file build was rejected: it unpacks about 300 MB into `%TEMP%` on every start, leaves that behind
   whenever the engine is killed, and would load a kernel driver from a temp folder.
2. `ambient-engine.exe` is copied to `app\src-tauri\binaries\ambient-engine-x86_64-pc-windows-msvc.exe` (the
   name Tauri wants for a sidecar) and `_internal\` next to it.
3. Tauri builds `ambient.exe` and the installers. Windows-only bundle settings are in
   `app\src-tauri\tauri.windows.conf.json`, which Tauri merges over `tauri.conf.json` on Windows only: the
   Mac build does not see it. It ships `_internal\` as resources beside the two executables.

Run from source instead: `uv sync`, `set PYTHONUTF8=1`, then `uv run python -m engine` (from a normal
terminal: Windows asks for permission, see below). `PYTHONUTF8` makes Python read and write UTF-8 the way it
does on macOS; the packaged engine has it built in.

## What the installer does

`Ambient_0.1.0_x64-setup.exe` installs for all users, so Windows asks for an administrator ("Do you want to
allow this app from an unknown publisher…", because nothing is signed). It then:

| Puts | Where |
|---|---|
| `ambient.exe` | `C:\Program Files\Ambient\`, the tray app (Tauri, WebView2) |
| `ambient-engine.exe`, `_internal\` | same folder: the engine, started and stopped by the tray app |
| `_internal\mitmproxy_windows\windows-redirector.exe`, `WinDivert.dll`, `WinDivert64.sys` | mitmproxy's capture helper and the WinDivert driver it loads, unmodified from the `mitmproxy-windows` wheel |
| `uninstall.exe`, a Start menu entry, optionally a desktop shortcut | |

It installs **no** driver, service, certificate, firewall rule or start-at-login entry. Program Files is
used on purpose: the helper runs elevated and loads a driver, so those files must not be writable by
ordinary programs. Before copying files (and before uninstalling) it stops a running engine and helper and
runs `sc stop WinDivert`, so the driver file is not in use (`packaging\windows\installer-hooks.nsh`).

`Ambient_0.1.0_x64_en-US.msi` installs the same files to the same place (`msiexec /i … /qn` and `/x` were
run: installs, the installed engine answers, uninstalls cleanly). It has no hooks, so quit Ambient before
upgrading or removing it that way.

Uninstalling removes the whole program folder (checked with the driver still loaded from it: the hook
unloads it first). It leaves `%LOCALAPPDATA%\Ambient` (database, logs, icon cache),
`%USERPROFILE%\.mitmproxy` (the certificate and its key) and the certificate's trust. To remove the trust:
`certutil -user -delstore Root mitmproxy`.

## What a person has to click

1. **Start Ambient** from the Start menu. The app and the engine run as the normal user.
2. **"Do you want to allow this app … windows-redirector.exe, Publisher: Unknown" → Yes.**
   (`screenshots/windows-07-uac-prompt.png`.) This is how mitmproxy 12 starts capture on Windows: the engine
   calls `ShellExecute` with the `runas` verb on `windows-redirector.exe`, passing the name of a named pipe
   (`\\.\pipe\mitmproxy-transparent-proxy-<engine pid>`). Only that helper is elevated. It opens the WinDivert
   driver (Windows loads `WinDivert64.sys` on first use), redirects other programs' connections to the
   engine over the pipe, and exits when the pipe closes. **Windows asks every time capture starts**: each
   launch, and each Resume after a Pause. A user without administrator rights is asked for an
   administrator's password each time.
   Answering No: the engine exits, the window says "Ambient is paused" and a notification explains
   (`screenshots/windows-09-permission-declined.png`). "Resume watching" in the tray menu asks again.
3. **"Trust Ambient's certificate" → Trust**, in the Ambient window, once. Windows shows its own
   "Security Warning" naming the certificate and its thumbprint; answer **Yes**
   (`screenshots/windows-06-certificate-prompt.png`). This is `certutil -user -addstore Root`: the
   current user's Trusted Root store, no administrator. The window stops asking as soon as the exact
   certificate is in a Root store (checked by content, not by name).
4. **The tray icon**: Windows 11 puts new tray icons in the overflow (the `^` next to the clock). Drag it
   onto the taskbar, or Settings → Personalization → Taskbar → Other system tray icons. Left click opens
   the panel above the taskbar; right click has Show, Open, Pause/Resume and Quit.

Quit and Pause end capture cleanly: the app closes the engine's stdin, the engine shuts down the way Ctrl-C
would, the helper exits and the network is as before. If the app dies, the engine notices (same pipe, plus
a parent-process check) and stops. If the engine is killed outright, the helper still exits.

## What was checked in the VM

- Engine from source in proxy mode and in local capture mode; the frozen engine alone with an empty model
  cache and `HF_HUB_OFFLINE=1` (ready in 7 s, `/api/triggers/suggest` answers with scores).
- Local capture as a normal user (UAC → Yes): `curl.exe`, Microsoft Edge, Firefox and Visual Studio Code
  decrypted and named correctly, with their own icons (128 px).
- A trigger bound to "open a page" on example.com fires from Edge, runs the stand-in agent, and a Windows
  notification appears. A trigger with "check the content" runs the local classifier (p = 0.965).
- Installer (wizard and silent), Start menu launch, tray icon, panel, menu, main window, Pause/Resume, Quit;
  browsing works after Quit. The same after a reboot of the VM. Uninstall. MSI install and uninstall.
- `curl https://github.com` (see the revocation note below), and that sites with bad certificates
  (expired, self-signed, unknown root) are still refused by the engine.

### Second run: rebuilt from commit 1302917, a real site, the trigger builder

Later the same day both installers were rebuilt in the VM from commit `1302917` (the API hardening, credential
masking, trigger ranking and "any website" changes), the `.exe` was installed silently over the first build,
and the installed app was checked again, this time on real pages in Microsoft Edge with no sign-in:

- Header says "Listening", tray icon present, `/api/setup` reports the certificate trusted and capture on.
  A request with `Host: evil.example` gets 403, as does one with a foreign `Origin` and a POST without
  `X-Ambient`; a normal request gets 200.
- `github.com/tauri-apps/tauri/pull/1`, `…/issues` and a Wikipedia article show up in Activity as GitHub and
  Wikipedia "in Microsoft Edge", kind "open a page", with the page titles
  (`screenshots/windows-13-real-site-activity.png`).
- `/api/triggers/suggest` for "I open a pull request" ticks the "any website" group (`*|VISIT`) and turns the
  meaning check on. Saved as an automatic trigger with the stand-in agent, it fired on pull requests #2 and #4
  (meaning check 95% and 97%) and not on the repository page (12%), rust-lang.org (0.1%) or a redirect (21%).
  The Windows notification banner appeared (`screenshots/windows-16-trigger-fired-on-pull-request.png`).
- In the window itself: N opens the builder, typing the sentence shows the same suggestion and the backtest
  (`screenshots/windows-14-builder-any-website.png`); "Not listed? Show me once" followed by a DuckDuckGo
  search in Edge lists what was just seen (`screenshots/windows-15-show-me-once.png`).
- Pause, Resume and Quit from the tray menu; after Quit no `ambient`, `ambient-engine` or
  `windows-redirector` process is left and browsing works. The WinDivert driver stays loaded
  (`sc query WinDivert` says RUNNING) until the next restart or an install/uninstall, which unloads it.
- Three capture starts in this run (one launch, two Resumes); the name-lookup stall described below did not
  happen on any of them (12 fresh lookups timed straight after each start, slowest 66 ms).

Not repeated in this run: the MSI was built but not installed again, and the uninstaller was not run.

### Third build: commit 1927856

Rebuilt in the VM from commit `1927856` (the second-review fixes and `osal.adopt_user_path()`, which changes
nothing on Windows), the `.exe` installed silently over the second build, and the installed app checked:

- Started with UAC → Yes; header "Listening", tray icon present (`screenshots/windows-17-rebuild-1927856.png`).
  `/api/setup` 200 with the certificate trusted and local capture on; `Host: evil.example` gets 403.
- `/api/triggers` lists the agents "Stand-in agent (testing)" (from `AMBIENT_AGENT_CMD`), then Claude Code,
  Codex and opencode, all three `available: false` (no real agent CLI is installed).
- `github.com/tauri-apps/tauri/pull/5` in Edge shows in `/api/activity` as GitHub in Microsoft Edge, "open a
  page", with the page title. An Edge already running from before capture started sent nothing through
  Ambient until it was restarted.
- Quit from the tray: no `ambient`, `ambient-engine` or `windows-redirector` process left, port 8787 closed,
  `curl` to GitHub and Wikipedia answers 200. No name-lookup stall: 12 fresh lookups straight after the start,
  none failed, slowest 743 ms.
- `win-gui` gotcha: while the app started by `win-gui` runs, its `AmbientGui` task stays "Running", so a second
  `win-gui` does nothing. Start other programs through a task with another name.

Not done: MSI install, uninstall. The installers below are now from this build.

## Known limitations

- **Nothing is signed.** UAC says "unknown publisher" for the installer and, on every start, for
  `windows-redirector.exe`. A downloaded installer will also meet SmartScreen ("Windows protected your PC" →
  More info → Run anyway); that was not seen here because the file was built inside the VM and has no
  download mark.
- **One permission prompt per capture start**, as above. Making it once-only would need a service or a
  scheduled task installed with administrator rights that starts the helper on request; that is a privilege
  boundary to design carefully and was not built.
- **Antivirus and WinDivert.** Microsoft Defender at its defaults allowed everything here. Other products are
  known to flag WinDivert (it is the same driver packet-filtering tools use) and may block or delete
  `WinDivert64.sys`; then the engine exits at start and Ambient shows "paused". Not tested with any
  third-party product. Not tested with Secure Boot or Memory Integrity (the VM has neither).
- **Name lookups can fail for the first half minute after capture starts.** The first one or two DNS
  lookups hang for 12 s each and fail ("could not resolve host"), and Windows then fails a few more at once
  before it recovers. Seen in about half of all starts, including the first start after a reboot, and
  equally with plain `mitmdump --mode local` and no Ambient code, so it is in mitmproxy's Windows capture
  (0.12.15). Once past it, 170 lookups in a row passed. A likely cause, not confirmed: the helper holds back
  packets of a flow until Windows reports the socket that owns it (`ConnectionState::Unknown` in
  `mitmproxy-windows/redirector/src/main2.rs`), and for sockets the DNS client already had open no such
  report comes. Not fixed here; it needs a change in the helper.
- **IPv6 that leads nowhere.** The VM's network (QEMU user-mode NAT) hands out an IPv6 address with no
  route. Without capture, apps fall back to IPv4 at once; with capture the connection is accepted locally
  first, so they wait for a timeout instead. IPv6 was switched off in the VM. Real dual-stack networks were
  not tested.
- **Apps that do not use the Windows certificate store are not read**, only counted: after one failed
  connection (three for a dropped handshake) the app, or the app and host, passes through untouched. In the
  VM: Edge's own services (`edge.microsoft.com`, `config.edge.skype.com`), and anything with its own trust
  list (Node, Python, Java, Git's OpenSSL, uv, cargo). Windows push, telemetry, Windows Update and Defender
  lookups pin their certificates and are passed through from the start (`osal.pinned_hosts`).
  Firefox **is** read: current Firefox trusts the Windows Root store by default. Chrome was not tested.
  Everything built on WebView2 (the Windows widgets, new Outlook, Teams) shows up as one app,
  "Microsoft Edge WebView2", split by site like a browser; when the widgets refuse the certificate for two
  hosts before any WebView2 app has accepted it, all of them pass through for a day.
- **Which servers the engine itself trusts.** mitmproxy checks the real server against the Mozilla list
  only. Windows trusts more (Microsoft's own roots, a company's private CA), and those sites answered 502
  under capture (`settings-win.data.microsoft.com`, `array601.prod.do.dsp.mp.microsoft.com`). The engine now
  checks against the Mozilla list plus the PC's Root and Intermediate stores (`osal.upstream_ca_bundle`,
  118 + 32 certificates in the VM), rebuilt at each start; both hosts verify with it, and expired or
  self-signed certificates are still refused. Roots that Windows would only download on first use are not
  in those stores yet.
- **Programs using Windows' own TLS with revocation checks** (`curl.exe`) refuse a certificate that names no
  revocation list. mitmproxy only names one when the real site's certificate does; github.com's does not, and
  `curl https://github.com` failed with `CRYPT_E_NO_REVOCATION_CHECK`. The engine now adds a revocation-list
  address to every certificate it mints on Windows and answers it itself (`osal.certs_need_crl`,
  `addon._crl_on_every_cert`). Fixed and checked for `curl.exe`; other strict clients were not tested.
- **Connections a local server accepts are captured too.** `sshd.exe` is excluded by default so a remote
  shell does not ride through the engine. Remote Desktop and other servers were not tested.
- **QUIC (HTTP/3) is dropped** so browsers fall back to TCP, as on macOS.
- Not tested: launching the app from an MSI install (the MSI was installed, its engine checked, and
  removed, all silently), local-storage readers (WhatsApp and the like), "Protect your coding agents" (`setx NODE_EXTRA_CA_CERTS`), a real agent CLI,
  a standard (non-administrator) account, Windows 10, ARM64 (WinDivert has no ARM64 driver).
- No start at login and no auto-update.
- **The VM froze once while Ambient was capturing** (03:38, about 13 minutes into a session: screen and
  clock stopped, QEMU at 200% CPU, no blue screen, nothing in the event log but "unexpected shutdown"). It
  had to be killed and restarted. One freeze in about an hour of capture in total; this VM also blue-screened
  once before any of this work (`docs/test-vms.md`), and it runs KVM nested under Hyper-V. Whether WinDivert
  had a part in it is not known. Worth a long run on real hardware before shipping.
- A notification banner is not shown while another app's banner is waiting on screen (in the VM a OneDrive
  "Turn On Windows Backup" prompt held Ambient's banners back until it was dismissed); they are in the
  notification centre meanwhile (`screenshots/windows-11-notifications.png`). Seen again in the second run:
  no banner for the first trigger, then, after "Clear all" in the notification centre, a banner for the next.
- The panel says "checked on this Mac" (text in `ui/panel.html`, shared by every OS; not changed here).

## What was changed inside the test VM

So the next person is not surprised: IPv6 is unbound from the network adapter; Edge has the policies
`HideFirstRunExperience` and `RestoreOnStartup=5`; VS Code (user install) and Firefox are installed; the
Ambient tray icon is promoted to the taskbar; the user variable `AMBIENT_AGENT_CMD` points at
`C:\ambient\scripts\standin_agent.py`, so the installed app answers triggers with the stand-in agent
(delete it with `reg delete HKCU\Environment /v AMBIENT_AGENT_CMD /f` and sign in again to use a real one);
Ambient's certificate is trusted for the user `ambient`; the repo copy and build tree are in `C:\ambient`,
scratch files in `C:\t`. Ambient 0.1.0 is installed from the `.exe` installer and not running. `C:\ambient`
is an unpacked `git archive` of commit `1927856` (without `docs/screenshots`; no `.git`; `SOURCE_COMMIT.txt`
names the commit) on top of the earlier build trees. The database also holds the test triggers of both runs ("windows test", "windows
semantic test", "pull request opened"). Firefox opens on a "Welcome to Firefox" terms dialog that was left
unanswered, so the second run used Edge only.

The two installers of the third build (commit `1927856`) are in `C:\ambient\dist` in the VM and on the Mac in
`dist-vm/` (git-ignored); they replace those of the second run:

| File | Bytes | SHA-256 |
|---|---|---|
| `Ambient_0.1.0_x64-setup.exe` | 129,402,674 | `c79263981e169fbfd985ee88dc5361ff3dcd4fae72c072a6257c7fa48866eb83` |
| `Ambient_0.1.0_x64_en-US.msi` | 148,310,643 | `a0a50cb9a6b49ce373401bab31f8423ec58194c8a083f04f9905046b9dd9b1bb` |

`win-pull` is far too slow for files this size (about 10 KB/s). What worked: send the file from the VM to a
`socat` listener in WSL on the box (`10.0.2.2` from inside the VM), then read it from the Mac with
`ssh <box> "wsl -d Ubuntu-24.04 -- cat /home/luqs1/ambient-vms/out/<file>" > dist-vm/<file>`. Over the
Tailscale relay that ran between 10 and 800 KB/s, and a second transfer at the same time slowed both to
about 10 KB/s, so copy one file at a time.

Driving the desktop from the Mac: the Windows permission prompt is on the secure desktop, where scripts in the
user's session cannot click; `vmctl keys win alt-y` on the box (a key press sent by QEMU) answers Yes. Tray
menu clicks only work when the right click and the menu click are sent by the same script run: starting
another program in between closes the menu.
