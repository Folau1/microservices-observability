#!/bin/sh
# Telegraf exec input. A missing or unexpected mount must not be measured as /.
set -eu
mountinfo=${MOUNTINFO_FILE:-/host/proc/1/mountinfo}
mounted_path=${LOGGING_HOST_PATH:-/var/lib/codex-elasticsearch}
volume_path=${LOGGING_VOLUME_PATH:-/host/logging-volume}
identity_dir=${LOGGING_IDENTITY_DIR:-/etc/telegraf/logging-volume-identity}
marker="$volume_path/.codex-logging-volume-id"
emit_absent() { printf '%s\n' 'logging_volume mount_present=0i'; exit 0; }
[ -r "$mountinfo" ] || emit_absent
[ -r "$identity_dir/source" ] && [ -r "$identity_dir/marker" ] || emit_absent
expected_source=$(cat "$identity_dir/source")
expected_marker=$(cat "$identity_dir/marker")
[ -n "$expected_source" ] && [ -n "$expected_marker" ] || emit_absent
actual=$(awk -v path="$mounted_path" '$5 == path { count++; for (i=6; i<=NF; i++) if ($i=="-") {found=$(i+1) " " $(i+2); break} } END {if (count==1) print found}' "$mountinfo")
[ "$actual" = "ext4 $expected_source" ] || emit_absent
[ -f "$marker" ] && [ -r "$marker" ] || emit_absent
[ "$(cat "$marker")" = "$expected_marker" ] || emit_absent
size=$(df -Pk "$volume_path" 2>/dev/null | awk 'NR==2 && $2 ~ /^[0-9]+$/ && $4 ~ /^[0-9]+$/ {printf "%.0f %.0f", $2*1024, $4*1024}')
set -- $size
if [ "$#" -ne 2 ]; then
  # Keep mount status, but no free/capacity series: an absent-series alert is required.
  printf '%s\n' 'logging_volume mount_present=1i'
  exit 0
fi
printf 'logging_volume mount_present=1i,capacity_bytes=%si,available_bytes=%si\n' "$1" "$2"
