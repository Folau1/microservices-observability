#!/usr/bin/env bash
# Run on node2 immediately before applying or starting the Vector DaemonSet.
set -euo pipefail
mount=/var/lib/codex-elasticsearch
data="$mount/vector"
fail() { printf '%s\n' "$*" >&2; exit 1; }
[[ -d "$data" ]] || fail "Missing Vector directory: $data"
[[ "$(readlink -e "$data")" == "$data" ]] || fail 'Vector path contains a symlink or is inaccessible'
[[ "$(findmnt -rn -o TARGET -M "$mount")" == "$mount" ]] || fail 'Dedicated filesystem is not mounted at the expected path'
[[ "$(findmnt -rn -o FSTYPE -M "$mount")" == ext4 ]] || fail 'Unexpected filesystem type'
[[ "$(findmnt -rn -o TARGET -T "$data")" == "$mount" ]] || fail 'Vector directory is not on the expected filesystem'
printf '%s\n' 'Vector data directory is on the dedicated filesystem.'
