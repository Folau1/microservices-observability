#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
mkdir "$tmp/volume" "$tmp/identity"
marker="$tmp/volume/.codex-logging-volume-id"
mounts="$tmp/mountinfo"
export MOUNTINFO_FILE="$mounts" LOGGING_VOLUME_PATH="$tmp/volume" LOGGING_IDENTITY_DIR="$tmp/identity"
expected='fixture-identity'
printf '%s\n' "$expected" > "$marker"
printf '%s\n' "$expected" > "$tmp/identity/marker"
printf '%s\n' '/dev/mapper/fixture-volume' > "$tmp/identity/source"
printf '%s\n' '50 31 252:1 / /var/lib/codex-elasticsearch rw - ext4 /dev/mapper/fixture-volume rw' > "$mounts"
result=$(sh "$here/check-logging-volume.sh")
printf '%s\n' "$result" | grep -Eq '^logging_volume mount_present=1i,capacity_bytes=[0-9]+i,available_bytes=[0-9]+i$'
mkdir "$tmp/bin"
printf '#!/bin/sh\nexit 1\n' > "$tmp/bin/df"
chmod +x "$tmp/bin/df"
[ "$(PATH="$tmp/bin:$PATH" sh "$here/check-logging-volume.sh")" = 'logging_volume mount_present=1i' ]
printf '%s\n' '50 31 252:1 / /other rw - ext4 /dev/mapper/fixture-volume rw' > "$mounts"
[ "$(sh "$here/check-logging-volume.sh")" = 'logging_volume mount_present=0i' ]
printf '%s\n' '50 31 252:1 / /var/lib/codex-elasticsearch rw - ext4 /dev/mapper/wrong rw' > "$mounts"
[ "$(sh "$here/check-logging-volume.sh")" = 'logging_volume mount_present=0i' ]
printf '%s\n' '50 31 252:1 / /var/lib/codex-elasticsearch rw - ext4 /dev/mapper/fixture-volume rw' '51 31 252:2 / /var/lib/codex-elasticsearch rw - ext4 /dev/mapper/wrong rw' > "$mounts"
[ "$(sh "$here/check-logging-volume.sh")" = 'logging_volume mount_present=0i' ]
printf '%s\n' '50 31 252:1 / /var/lib/codex-elasticsearch rw - ext4 /dev/mapper/fixture-volume rw' > "$mounts"
printf '%s\n' 'wrong' > "$marker"
[ "$(sh "$here/check-logging-volume.sh")" = 'logging_volume mount_present=0i' ]
rm "$marker"
[ "$(sh "$here/check-logging-volume.sh")" = 'logging_volume mount_present=0i' ]
rm "$mounts"
[ "$(sh "$here/check-logging-volume.sh")" = 'logging_volume mount_present=0i' ]
printf '%s\n' 'mount, df failure, missing/stacked mount, wrong source, missing/bad marker and missing mountinfo checks passed'
