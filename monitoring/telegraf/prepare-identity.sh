#!/usr/bin/env bash
# First-time preparation, on the intended node as root. No credentials in Git.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
expected_device=${1:?Pass the previously inspected device path as the first argument.}
mount_path=/var/lib/codex-elasticsearch
state=/var/lib/codex-logging-monitor-identity
marker="$mount_path/.codex-logging-volume-id"
for cmd in k3s openssl findmnt mountpoint readlink python3; do command -v "$cmd" >/dev/null || { echo "Missing command: $cmd" >&2; exit 1; }; done
mountpoint -q "$mount_path" || { echo 'Logging filesystem is not mounted.' >&2; exit 1; }
source=$(findmnt -n -M "$mount_path" -o SOURCE)
uuid=$(findmnt -n -M "$mount_path" -o UUID)
fstype=$(findmnt -n -M "$mount_path" -o FSTYPE)
fstab_source=$(findmnt --fstab -n -M "$mount_path" -o SOURCE)
fstab_type=$(findmnt --fstab -n -M "$mount_path" -o FSTYPE)
[[ -n "$uuid" && "$fstype" == ext4 && "$fstab_type" == ext4 && "$fstab_source" == "UUID=$uuid" ]] || { echo 'Mount and fstab do not identify the same ext4 filesystem.' >&2; exit 1; }
[[ -b "$expected_device" && $(readlink -f "$source") == "$(readlink -f "$expected_device")" ]] || { echo 'Mounted source is not the supplied device.' >&2; exit 1; }
[[ ! -e "$state" && ! -L "$state" && ! -e "$marker" && ! -L "$marker" ]] || { echo 'Identity or marker exists; inspect it instead of overwriting.' >&2; exit 1; }
secret_names=$(k3s kubectl --request-timeout=10s -n observability get secrets -o name) || { echo 'Cannot inspect existing Secrets; stopping.' >&2; exit 1; }
if grep -Fxq 'secret/logging-volume-identity' <<< "$secret_names"; then
  echo 'Identity Secret already exists; do not replace it automatically.' >&2; exit 1
fi
umask 077
mkdir -m 0700 "$state"
openssl rand -hex 32 > "$state/marker"
printf '%s\n' "$source" > "$state/source"
chmod 0600 "$state/marker" "$state/source"
# Recheck just before creating the marker, to reduce (not eliminate) races.
[[ $(findmnt -n -M "$mount_path" -o UUID) == "$uuid" ]] || { echo 'Mount changed; inspect partial identity manually.' >&2; exit 1; }
python3 - "$state/marker" "$marker" <<'PY'
import os, pathlib, sys
payload = pathlib.Path(sys.argv[1]).read_bytes()
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
with os.fdopen(fd, 'wb') as out:
    out.write(payload)
    os.fchmod(out.fileno(), 0o444)
    out.flush()
    os.fsync(out.fileno())
PY
k3s kubectl -n observability create secret generic logging-volume-identity \
  --from-file=marker="$state/marker" --from-file=source="$state/source"
echo 'Volume identity and Secret prepared. Keep the private identity out of Git.'
