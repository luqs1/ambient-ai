# Shared settings for the test-VM helpers. Sourced by the scripts next to it; see docs/test-vms.md.
VM_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[ -f "$VM_DIR/keys/box.env" ] && . "$VM_DIR/keys/box.env"      # git-ignored: AMBIENT_BOX=user@host
BOX=${AMBIENT_BOX:?set AMBIENT_BOX=user@host of the machine running the VMs, or put it in packaging/vm/keys/box.env}
WSL_DISTRO=${AMBIENT_WSL_DISTRO:-Ubuntu-24.04}
BOX_ROOT=/home/luqs1/ambient-vms               # where the VMs live inside WSL
KEY=$VM_DIR/keys/id_ed25519
BOX_SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 "$BOX")

# box <command...>: run one simple command inside WSL on the box. The line passes through cmd.exe first,
# so no quotes, pipes or && here; use box_sh for anything more.
box() { "${BOX_SSH[@]}" "wsl -d $WSL_DISTRO -- $*"; }
# box_sh: run the bash script given on stdin inside WSL on the box.
box_sh() { "${BOX_SSH[@]}" "wsl -d $WSL_DISTRO -- bash -s"; }

# vm_target win|lin: sets VM_USER and VM_PORT.
vm_target() {
  case "$1" in
    win) VM_USER=ambient; VM_PORT=2322 ;;
    lin) VM_USER=ambient; VM_PORT=2422 ;;
    *) echo "unknown vm '$1' (win|lin)" >&2; exit 2 ;;
  esac
  # The VM's sshd listens on 127.0.0.1:<port> inside WSL only. Windows cannot reach that loopback port itself,
  # so hop: SSH to the Windows host, which runs socat inside WSL to connect to the port.
  VM_SSH_OPTS=(-i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=30
    -o "ProxyCommand=ssh -o BatchMode=yes -o ConnectTimeout=15 $BOX \"wsl -d $WSL_DISTRO -- socat - TCP:127.0.0.1:%p\""
    -o "UserKnownHostsFile=$VM_DIR/keys/known_hosts"
    -o StrictHostKeyChecking=accept-new -o LogLevel=ERROR)
}

# vm_exec win|lin <command...>
vm_exec() { local n=$1; shift; vm_target "$n"; ssh "${VM_SSH_OPTS[@]}" -p "$VM_PORT" "$VM_USER@127.0.0.1" "$@"; }

# vm_push win|lin <local> <remote>   /   vm_pull win|lin <remote> <local>   (files or folders)
vm_push() { local n=$1; vm_target "$n"; scp -r -q "${VM_SSH_OPTS[@]}" -P "$VM_PORT" "$2" "$VM_USER@127.0.0.1:$3"; }
vm_pull() { local n=$1; vm_target "$n"; scp -r -q "${VM_SSH_OPTS[@]}" -P "$VM_PORT" "$VM_USER@127.0.0.1:$2" "$3"; }

# vm_shot win|lin [out.png]: screenshot of the VM's display, taken by QEMU (works at any point, even during boot).
vm_shot() {
  local n=$1 out=${2:-$1-$(date +%Y%m%d-%H%M%S).png}
  box "$BOX_ROOT/bin/vmctl shot $n" > "$out"
  if [ "$(head -c 4 "$out" | xxd -p)" != "89504e47" ]; then echo "screenshot failed (is the VM running?)" >&2; rm -f "$out"; return 1; fi
  echo "$out"
}
