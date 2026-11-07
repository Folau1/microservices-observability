#!/usr/bin/env bash
set -euo pipefail
base=/var/lib/codex-elasticsearch
data=$base/data
[[ $(hostname) == node2 ]] || { echo 'Wrong node.' >&2; exit 1; }
[[ ! -L "$base" ]] || { echo 'Mountpoint is symlinked.' >&2; exit 1; }
mountpoint -q "$base" || { echo 'Separate Elasticsearch filesystem is not mounted.' >&2; exit 1; }
[[ $(findmnt -rn -M "$base" -o FSTYPE) == ext4 ]] || { echo 'Expected ext4.' >&2; exit 1; }
[[ $(stat -c %d "$base") != "$(stat -c %d /)" ]] || { echo 'Mount is on the root device.' >&2; exit 1; }
[[ -d "$data" && ! -L "$data" ]] || { echo 'Data directory missing or symlinked.' >&2; exit 1; }
[[ $(stat -c %d "$base") == "$(stat -c %d "$data")" ]] || { echo 'Data is not on expected filesystem.' >&2; exit 1; }
[[ $(stat -c '%u:%g' "$data") == 1000:0 ]] || { echo 'Expected new data directory owned by 1000:0.' >&2; exit 1; }
size_kib=$(df -Pk "$base" | awk 'END {print $2}')
free_kib=$(df -Pk "$base" | awk 'END {print $4}')
(( size_kib >= 3145728 && size_kib <= 5242880 )) || { echo 'Expected a 3–5 GiB bounded filesystem.' >&2; exit 1; }
(( free_kib >= 1048576 )) || { echo 'Less than 1 GiB free on Elasticsearch filesystem.' >&2; exit 1; }
maps=$(sysctl -n vm.max_map_count)
(( maps >= 1048576 )) || { echo 'vm.max_map_count must be at least 1048576.' >&2; exit 1; }
printf 'Validated mount: %s, size %s KiB, free %s KiB.\n' "$base" "$size_kib" "$free_kib"
