#!/usr/bin/env bash
# Provision the Ubuntu test VM: XFCE desktop with auto-login, then the build toolchain.
# Run inside the VM as the 'ambient' user (passwordless sudo). Log: ~/provision.log, done marker: ~/provision.done
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
APT="sudo -E apt-get -y -o Dpkg::Options::=--force-confold -o DPkg::Lock::Timeout=300"

$APT update
# Desktop (XFCE with the classic panel + status tray) and display manager.
$APT install xubuntu-core lightdm xfce4-terminal dbus-x11 x11-xserver-utils
# Build tools, Tauri v2 Linux deps, GUI test tools.
$APT install build-essential curl wget file git pkg-config unzip ca-certificates \
  libwebkit2gtk-4.1-dev libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev \
  xdotool scrot
# Nothing should lock or blank the screen during a test. (The autostart entry below also makes the ACPI power
# button shut down without asking, so `vm stop lin` is a clean shutdown.)
$APT purge xfce4-screensaver light-locker || true

session=xfce; [ -f /usr/share/xsessions/xubuntu.desktop ] && session=xubuntu
sudo mkdir -p /etc/lightdm/lightdm.conf.d
printf '[Seat:*]\nautologin-user=%s\nautologin-user-timeout=0\nautologin-session=%s\nuser-session=%s\n' "$USER" "$session" "$session" \
  | sudo tee /etc/lightdm/lightdm.conf.d/50-ambient-autologin.conf
echo /usr/sbin/lightdm | sudo tee /etc/X11/default-display-manager
sudo systemctl set-default graphical.target
mkdir -p ~/.config/autostart
cat > ~/.config/ambient-session.sh <<'D'
#!/bin/sh
# Runs at desktop login in the test VM: never blank the screen; ACPI power button = shut down without asking.
xset s off -dpms
q() { xfconf-query -c xfce4-power-manager -p "/xfce4-power-manager/$1" -n -t "$2" -s "$3"; }
q dpms-enabled bool false
q blank-on-ac int 0
q power-button-action uint 4
D
chmod +x ~/.config/ambient-session.sh
cat > ~/.config/autostart/ambient-session.desktop <<D
[Desktop Entry]
Type=Application
Name=Ambient test VM session settings
Exec=$HOME/.config/ambient-session.sh
D

# Node LTS from NodeSource's apt repo.
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
$APT install nodejs

# User-level toolchains.
curl -LsSf https://astral.sh/uv/install.sh | sh
~/.local/bin/uv python install 3.13 --default || ~/.local/bin/uv python install 3.13
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
curl -fsSL https://bun.sh/install | bash

# Make the tools visible to non-interactive SSH commands and to the desktop session.
echo "PATH=\"$HOME/.cargo/bin:$HOME/.local/bin:$HOME/.bun/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin\"" | sudo tee /etc/environment

date -Is > ~/provision.done
sudo systemctl reboot
