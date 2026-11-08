#!/usr/bin/env bash
# Exercise AppServiceDown without stopping the application. This temporarily
# adds one deliberately unreachable target, and restores only that config key.
set -Eeuo pipefail

namespace=${MONITORING_NAMESPACE:-observability}
configmap=${PROMETHEUS_CONFIGMAP:-prometheus-config}
target_name=controlled-alert-test
sync_timeout=${CONFIG_SYNC_TIMEOUT_SECONDS:-180}
alert_timeout=${ALERT_TIMEOUT_SECONDS:-180}
[[ "$sync_timeout" =~ ^[0-9]+$ && "$alert_timeout" =~ ^[0-9]+$ ]] || { echo 'Timeouts must be whole seconds.' >&2; exit 1; }

for tool in python3 curl sha256sum mktemp; do
  command -v "$tool" >/dev/null || { echo "Required command not found: $tool" >&2; exit 1; }
done
case "${KUBECTL_MODE:-kubectl}" in
  kubectl) kube() { command kubectl "$@"; } ;;
  k3s) sudo -n true || { echo 'Passwordless sudo for K3s is required for this test.' >&2; exit 1; }
       kube() { sudo -n k3s kubectl "$@"; } ;;
  *) echo 'KUBECTL_MODE must be kubectl or k3s.' >&2; exit 1 ;;
esac

umask 077
tmp=$(mktemp -d "${TMPDIR:-/tmp}/codex-alert-test.XXXXXXXX")
changed=0

wait_config() {
  local expected deadline actual
  expected=$(sha256sum "$1" | cut -d ' ' -f 1)
  deadline=$((SECONDS + sync_timeout))
  while (( SECONDS < deadline )); do
    if kube -n "$namespace" exec deployment/prometheus -- cat /etc/prometheus/prometheus.yml > "$tmp/current-mounted" 2>/dev/null; then
      actual=$(sha256sum "$tmp/current-mounted" | cut -d ' ' -f 1)
      [[ "$actual" == "$expected" ]] && return 0
    fi
    sleep 3
  done
  echo 'Timed out waiting for the Prometheus ConfigMap volume to update.' >&2
  return 1
}

reload_config() {
  kube -n "$namespace" exec deployment/prometheus -- /bin/sh -c 'kill -HUP 1' >/dev/null
}

alert_firing() {
  # Exit codes: 0=firing, 1=absent, 2=API/error, 3=present but pending.
  curl -fsS --max-time 5 "$prom_url/api/v1/alerts" -o "$tmp/alerts.json" 2>/dev/null || return 2
  python3 - "$tmp/alerts.json" <<'PY'
import json,sys
try:
    with open(sys.argv[1],encoding="utf-8") as f: data=json.load(f)
except (OSError,ValueError):
    raise SystemExit(2)
if data.get("status") != "success": raise SystemExit(2)
alerts=data.get("data",{}).get("alerts",[])
matches=[a for a in alerts if a.get("labels",{}).get("alertname")=="AppServiceDown" and a.get("labels",{}).get("service")=="controlled-alert-test"]
if any(a.get("state")=="firing" for a in matches): raise SystemExit(0)
raise SystemExit(3 if matches else 1)
PY
}

wait_alert() {
  local desired=$1 deadline=$((SECONDS + alert_timeout)) state
  while (( SECONDS < deadline )); do
    state=0
    alert_firing || state=$?
    if [[ "$desired" == firing && "$state" -eq 0 ]] || [[ "$desired" == absent && "$state" -eq 1 ]]; then
      return 0
    fi
    sleep 3
  done
  echo "Timed out waiting for test alert state: $desired." >&2
  return 1
}

cleanup() {
  local rc=$? cleanup_failed=0 current original staged
  trap - EXIT INT TERM
  set +e
  if (( changed )); then
    echo 'Restoring original Prometheus scrape configuration...'
    if ! kube -n "$namespace" get configmap "$configmap" -o json > "$tmp/current-api.json"; then
      echo 'Could not inspect the ConfigMap; backup retained for manual recovery.' >&2
      cleanup_failed=1
    else
      python3 - "$tmp/current-api.json" "$tmp/current-api.yml" <<'PY'
import json,sys
with open(sys.argv[1], encoding="utf-8") as f: data=json.load(f)
with open(sys.argv[2], "w", encoding="utf-8", newline="") as f: f.write(data["data"]["prometheus.yml"])
PY
      if (( $? != 0 )); then
        echo 'Could not read current scrape configuration; backup retained.' >&2
        cleanup_failed=1
      else
        current=$(sha256sum "$tmp/current-api.yml" | cut -d ' ' -f 1)
        original=$(sha256sum "$tmp/original.yml" | cut -d ' ' -f 1)
        staged=$(sha256sum "$tmp/staged.yml" | cut -d ' ' -f 1)
        if [[ "$current" == "$staged" ]]; then
          if ! kube -n "$namespace" patch configmap "$configmap" --type=json --patch-file "$tmp/restore.json" >/dev/null; then
            echo 'Atomic restore failed; another change may have occurred. Backup retained.' >&2
            cleanup_failed=1
          fi
        elif [[ "$current" != "$original" ]]; then
          echo 'Scrape configuration changed concurrently; refusing to overwrite it. Backup retained.' >&2
          cleanup_failed=1
        fi
      fi
    fi
    if (( ! cleanup_failed )); then
      wait_config "$tmp/original.yml" && reload_config && wait_alert absent || cleanup_failed=1
    fi
  fi
  if (( cleanup_failed )); then
    echo "Cleanup incomplete. Original scrape configuration and patches retained in $tmp" >&2
    rc=1
  else
    rm -rf -- "$tmp"
    (( changed )) && echo 'Original configuration restored; test alert is absent.'
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ip=$(kube -n "$namespace" get service prometheus -o jsonpath='{.spec.clusterIP}')
[[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo 'Prometheus ClusterIP is not an IPv4 address.' >&2; exit 1; }
prom_url=${PROMETHEUS_URL:-http://$ip:9090}
curl -fsS --max-time 5 "$prom_url/-/ready" >/dev/null
kube -n "$namespace" get configmap "$configmap" -o json > "$tmp/original-api.json"

python3 - "$tmp" "$target_name" <<'PY'
import json,re,sys
from pathlib import Path
root=Path(sys.argv[1]); target=sys.argv[2]
obj=json.loads((root/"original-api.json").read_text(encoding="utf-8"))
original=obj["data"]["prometheus.yml"]
if target in original:
    raise SystemExit("Test target already exists; refusing to change configuration.")
pattern=r"(?m)^([ ]*)-[ ]+job_name:[ ]*['\"]?apps['\"]?[ ]*\n\1  static_configs:[ ]*\n"
matches=list(re.finditer(pattern, original))
if len(matches)!=1:
    raise SystemExit("This live test requires the earlier static apps job; Pod discovery is not supported. Use promtool test rules instead.")
m=matches[0]; indent=m.group(1)
item=f"{indent}    - targets: ['127.0.0.1:1']\n{indent}      labels: {{service: {target}}}\n"
staged=original[:m.end()]+item+original[m.end():]
for name,value in (("original.yml",original),("staged.yml",staged)):
    (root/name).write_text(value,encoding="utf-8",newline="")
def patch(before,after):
    return [{"op":"test","path":"/data/prometheus.yml","value":before},
            {"op":"replace","path":"/data/prometheus.yml","value":after}]
(root/"apply.json").write_text(json.dumps(patch(original,staged)),encoding="utf-8")
(root/"restore.json").write_text(json.dumps(patch(staged,original)),encoding="utf-8")
PY

# Mark cleanup necessary before patching: a network failure can hide a
# successful API write. JSON Patch's test operation prevents lost updates.
changed=1
kube -n "$namespace" patch configmap "$configmap" --type=json --patch-file "$tmp/apply.json" >/dev/null
wait_config "$tmp/staged.yml"
reload_config
wait_alert firing
echo 'PASS: AppServiceDown fired for the isolated unreachable test target.'
