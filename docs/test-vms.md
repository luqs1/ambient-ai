# Test VMs: Windows 11 and Ubuntu 24.04

Two disposable virtual machines for installing and testing Ambient (tray app, engine, root CA, traffic
interception) without touching a real machine's certificate store or security settings.

They run on the Windows desktop `kismet-lianli`, inside WSL2, under QEMU/KVM. Nothing was changed on the
Windows host itself. Everything is driven from this Mac with the scripts in `packaging/vm/`.

Built 2026-11-10.

## The two machines

| | Windows VM (`win`) | Ubuntu VM (`lin`) |
|---|---|---|
| OS | Windows 11 Enterprise Evaluation, 26H2 (build 26300), unactivated, 90-day licence (expires about 2027-02-08) | Ubuntu 24.04.5, kernel 6.8.0-147-generic |
| Desktop | Auto-logon to the desktop; system tray live | XFCE (Xubuntu core) with LightDM auto-login; panel has the status tray (classic tray + StatusNotifier/AppIndicator) |
| User | `ambient`, local administrator; UAC, Defender and firewall left at their defaults | `ambient`, passwordless `sudo` |
| SSH | OpenSSH server with key auth (password auth also left on; the port is only reachable from WSL loopback), default shell PowerShell, sessions are elevated | sshd, key only (password login off) |
| Size | 4 vCPU, 8 GB RAM, 64 GB disk (sparse) | 4 vCPU, 6 GB RAM, 64 GB disk (sparse) |
| Firmware and devices | UEFI (OVMF), NVMe disk, e1000e network card, plain VGA. No TPM, no Secure Boot | BIOS, virtio disk and network, virtio-vga |
| Source image | ISO from Microsoft's Evaluation Center | Cloud image from cloud-images.ubuntu.com (SHA-256 checked against Canonical's list) |

Passwords for the two `ambient` users are in `packaging/vm/keys/passwords.env`. The SSH keypair is
`packaging/vm/keys/id_ed25519`. That folder is git-ignored.

## Commands (run from the Mac)

All scripts live in `packaging/vm/`. Each one is a single line to use.

| Task | Windows | Ubuntu |
|---|---|---|
| Run a command | `packaging/vm/win-exec 'Get-Service sshd'` (PowerShell, already elevated) | `packaging/vm/lin-exec 'uname -r'` (prefix `sudo` for root) |
| Copy in | `packaging/vm/win-push ./dist C:/Users/ambient/dist` | `packaging/vm/lin-push ./dist /home/ambient/dist` |
| Copy out | `packaging/vm/win-pull C:/Users/ambient/out.log ./out.log` | `packaging/vm/lin-pull /home/ambient/out.log ./out.log` |
| Screenshot to a PNG | `packaging/vm/win-shot win.png` | `packaging/vm/lin-shot lin.png` |
| Start a program on the visible desktop | `packaging/vm/win-gui notepad.exe` (`-e` to run elevated) | `packaging/vm/lin-gui xfce4-terminal` |
| Start / stop / restart | `packaging/vm/vm start win`, `vm stop win`, `vm restart win` | `packaging/vm/vm start lin`, `vm stop lin`, `vm restart lin` |
| Status of both | `packaging/vm/vm status` | |

Notes on these:

- Push and pull take files or whole folders (they use `scp -r`).
- A screenshot is taken by QEMU from outside the guest, so it works during boot, on a lock screen or on a
  blue screen. With no file name it writes `win-<timestamp>.png` in the current folder.
- A program started from `win-exec` has no desktop: windows and tray icons never appear. Use `win-gui`
  for anything with a UI. It runs the program through a scheduled task in the logged-on session.
- On Ubuntu, one-off X commands work straight from `lin-exec` with `DISPLAY=:0`, for example
  `lin-exec 'DISPLAY=:0 xdotool search --name Ambient'` or `lin-exec 'DISPLAY=:0 scrot /tmp/s.png'`.
- `vm stop` is a clean shutdown (ACPI power button, waits up to 150 s, then powers off). `vm kill` pulls
  the plug.
- Scripts fed on stdin work: `lin-exec bash -s < script.sh` and
  `win-exec 'powershell -NoProfile -Command -' < script.ps1`.

### Do the VMs survive the SSH session closing?

Yes. Each VM is a systemd service inside WSL (`ambient-vm@win`, `ambient-vm@lin`), not a child of any SSH
session. WSL on this box never idles out, so they keep running until stopped.

They do **not** start by themselves after the PC or WSL restarts. Start them with `vm start win` and
`vm start lin`, or make that automatic with `vm enable win` and `vm enable lin` (`vm disable` undoes it).

## Toolchains installed

Ubuntu VM:

| Tool | Version |
|---|---|
| Python | 3.13.18 (uv-managed; `python`, `python3.13`) |
| uv | 0.13.7 |
| Rust | rustc 1.99.1, cargo 1.99.1 (stable, via rustup) |
| Node | v26.13.0 (LTS, NodeSource apt repo), npm 11.20.0 |
| bun | 1.4.4 |
| git | 2.43.0 |
| Tauri v2 build deps | `libwebkit2gtk-4.1-dev` (2.52.6), `build-essential`, `curl`, `wget`, `file`, `libxdo-dev`, `libssl-dev`, `libayatana-appindicator3-dev`, `librsvg2-dev` |
| GUI test tools | `xdotool`, `scrot` |

Windows VM:

| Tool | Version |
|---|---|
| Python | 3.13.18 (uv-managed; `python`, `python3.13`) |
| uv | 0.13.7 |
| Rust | rustc 1.99.1, cargo 1.99.1, toolchain `stable-x86_64-pc-windows-msvc` |
| MSVC | Visual Studio Build Tools 2022 (17.14.44), "Desktop development with C++" workload with its recommended parts (compiler, Windows SDK) |
| Node | v26.13.0 (LTS, official MSI), npm 11.20.0 |
| bun | 1.4.4 |
| git | 2.56.0.windows.4 |
| WebView2 runtime | 157.0.4341.42 (ships with Windows 11) |

A `cargo new` + `cargo build` hello-world compiled, linked and ran on both VMs.

On both, the tools are on `PATH` for plain `win-exec` / `lin-exec` commands.

## How it is laid out on the box

Everything is inside WSL (`Ubuntu-24.04`, user `luqs1`):

```
~/ambient-vms/
  vm.conf            sizes and ports; edit, then `vm restart <name>`
  bin/               run-vm, vmctl, create-vm, qmp.py and the templates (a copy of packaging/vm/box/)
  iso/               win11-eval.iso (7.7 GB), noble-cloudimg.img (0.6 GB), SHA256SUMS
  win/               disk.qcow2, OVMF_VARS.fd, QEMU sockets and pid file
  lin/               disk.qcow2, seed.iso (cloud-init), serial.log, QEMU sockets and pid file
  shots/             last screenshot of each VM
/etc/systemd/system/ambient-vm@.service     the one unit file, used for both VMs
```

Installed in WSL for this: the `genisoimage` package. QEMU, OVMF and socat were already there.

Ports, all bound to `127.0.0.1` inside WSL only (nothing is exposed to the LAN or the tailnet):

| Port | What |
|---|---|
| 2322 | Windows VM SSH |
| 3390 | Windows VM RDP (enabled in the guest, not used by the scripts) |
| 5911 | Windows VM screen over VNC (no password) |
| 2422 | Ubuntu VM SSH |
| 5912 | Ubuntu VM screen over VNC (no password) |

### How the Mac reaches a VM

`ssh` to the Windows host over Tailscale, which runs `socat` inside WSL to connect to the VM's forwarded
SSH port. This is the `ProxyCommand` in `packaging/vm/_lib.sh`. Windows itself cannot reach WSL's loopback
ports, so a plain `ssh -J` through the host does not work.

To watch a VM live, tunnel its VNC port through the VM's own SSH connection (from inside a guest,
`10.0.2.2` is the WSL loopback) and point a VNC client at `127.0.0.1:5912`:

```
source packaging/vm/_lib.sh; vm_target lin; ssh "${VM_SSH_OPTS[@]}" -p $VM_PORT -N -L 5912:10.0.2.2:5912 ambient@127.0.0.1
```

Use `win` and `5911` for Windows. The tunnel was checked; no VNC client was tried. macOS Screen Sharing
is known to be fussy about VNC servers with no password, so a client such as TigerVNC may be needed.

## Resource use

Measured with both VMs up and idle after setup:

| | Windows VM | Ubuntu VM |
|---|---|---|
| Memory held (QEMU resident) | 8.1 GB (all of its 8 GB, always) | 1.6 GB (up to 6 GB under load) |
| CPU when idle | about 10% of one core | under 1% of one core |
| Disk used on the box | 22 GB | 6.7 GB |

Plus 8.3 GB for the two install images in `iso/`: 37 GB in total. With both VMs up, WSL as a whole held
12 GB and the Windows host had 8.4 GB of its 31 GB free.

- The box has 31 GB RAM and WSL is capped at 20 GB (`.wslconfig`). Both VMs together can take up to 14 GB
  of that. Windows touches all 8 GB as soon as it boots; Ubuntu grows on demand and gives free pages back.
- No GPU is used. Display is software-rendered in both guests.
- VM disks are opened with `cache=none`. With the default cache mode, guest disk writes filled WSL's page
  cache up to the 20 GB cap and left the Windows host with under 1 GB free. If the host ever looks short of
  memory, `sudo sh -c 'echo 1 > /proc/sys/vm/drop_caches'` inside WSL hands cached memory back.
- Stop a VM when it is not needed (`vm stop win`) to hand its memory back.
- Disks are sparse qcow2 files that grow up to 64 GB each. They live in the WSL virtual disk, which sits
  on `C:` (about 280 GB free when this was built).

## Rebuild or reset a VM

A rebuild wipes the VM's disk and reinstalls from the image, using the key and passwords in
`packaging/vm/keys/`.

```
FORCE=1 packaging/vm/setup-box lin      # about 1 minute: boots the cloud image
packaging/vm/lin-push packaging/vm/box/lin-provision.sh /home/ambient/lin-provision.sh
packaging/vm/lin-exec 'tmux new-session -d -s provision "bash ~/lin-provision.sh > ~/provision.log 2>&1"'
                                        # desktop + toolchain, about 5 minutes, reboots at the end

FORCE=1 packaging/vm/setup-box win      # unattended install, about 20 minutes until SSH answers
packaging/vm/setup-box forget-secrets   # removes the answer file (it holds the VM password) from the box
packaging/vm/win-toolchain              # build tools, about 10 minutes, reboots at the end;
                                        # check with: packaging/vm/win-toolchain status
```

`packaging/vm/setup-box files` only pushes the scripts in `packaging/vm/box/` to the box.

For a quick reset point without reinstalling, stop the VM and copy its `disk.qcow2` aside on the box.

## Delete everything

```
ssh "$AMBIENT_BOX" "wsl -d Ubuntu-24.04 -u root -- systemctl disable --now ambient-vm@win ambient-vm@lin"
ssh "$AMBIENT_BOX" "wsl -d Ubuntu-24.04 -u root -- rm -f /etc/systemd/system/ambient-vm@.service"
ssh "$AMBIENT_BOX" "wsl -d Ubuntu-24.04 -u root -- systemctl daemon-reload"
ssh "$AMBIENT_BOX" "wsl -d Ubuntu-24.04 -- rm -rf /home/luqs1/ambient-vms"
ssh "$AMBIENT_BOX" "wsl -d Ubuntu-24.04 -u root -- apt-get remove -y genisoimage"    # optional
rm -rf ~/labs/ambient/packaging/vm/keys          # on the Mac: the VM keypair, known_hosts and passwords
```

To keep the VMs but free 8 GB, delete `~/ambient-vms/iso/` on the box. A later rebuild downloads the
images again from the URLs in `vm.conf`.

## Limitations and things to know

- **One blue screen was seen on the Windows VM**, about 12 minutes after first logon, during the first
  toolchain run (stop code 0xC8, IRQL_UNEXPECTED_VALUE, in a `Wof.sys` work item). The VM rebooted by
  itself and logged back on. After that the CPU settings were changed to the standard set for Windows
  guests on KVM (all Hyper-V enlightenments, nested virtualization hidden from the guest).
  Since that change it ran the Visual Studio
  Build Tools install, a Rust build, a guest reboot and a stop/start with no further crash. That is about
  15 minutes of evidence, not proof that it is fixed.
  To check for new ones: `packaging/vm/win-exec 'Get-WinEvent -FilterHashtable @{LogName="System"; Id=1001} -MaxEvents 5'`.
  KVM here runs nested under Hyper-V (WSL2), which is the less-travelled path for Windows guests.
- **The Windows VM has no TPM and no Secure Boot.** Setup was told to skip those checks. Anything in
  Ambient that depends on them cannot be tested here.
- **Windows evaluation licence lasts 90 days.** After that it nags and shuts down hourly. Rebuild it.
- **Windows disk and network are emulated NVMe and e1000e**, chosen because Windows has drivers for them
  built in. They are slower than virtio. Builds work, just not at full speed.
- **Networking is QEMU user-mode NAT.** Guests reach the internet over TCP and UDP (no ping). Guest
  address is `10.0.2.15`. Nothing can connect into a guest except through the forwarded ports above.
  A guest *can* reach services listening on WSL's loopback via `10.0.2.2` (for example the agentbus
  daemon on port 7777), and the home LAN, the same as any program in WSL can.
- **Copying is slow**, about 2.8 MB/s, because the Tailscale link between this Mac and the box was going
  through a relay (`lhr`) rather than direct. A 30 MB folder took 11 s.
- **Ubuntu's network is managed by systemd-networkd** (the cloud image default), so the NetworkManager
  tray icon shows "disconnected" even though the network works.
- **Ubuntu has no web browser installed.** Add one with `lin-exec 'sudo snap install firefox'` if a test
  needs it.
- **No auto-start** after the PC reboots (see above).
- **PowerShell script policy in the Windows VM is `RemoteSigned`** (changed from the default so `npm`
  works from `win-exec`). A clean customer machine has the stricter default.
- **The two VM passwords exist in three places:** `keys/passwords.env` on the Mac, the Windows guest's
  own auto-logon setting, and (as a hash only) the Ubuntu cloud-init seed on the box.
