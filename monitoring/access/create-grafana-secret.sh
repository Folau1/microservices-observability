#!/usr/bin/env bash
set -euo pipefail
case "${KUBECTL_MODE:-kubectl}" in
  kubectl) kube() { command kubectl "$@"; } ;;
  k3s) kube() { sudo k3s kubectl "$@"; } ;;
  *) echo 'KUBECTL_MODE must be kubectl or k3s' >&2; exit 2 ;;
esac
command -v openssl >/dev/null
kube auth can-i get secrets -n observability --quiet || { echo 'Cannot inspect Secrets.' >&2; exit 1; }
kube auth can-i create secrets -n observability --quiet || { echo 'Cannot create Secrets.' >&2; exit 1; }
secret_dir="${XDG_STATE_HOME:-$HOME/.local/state}/microservices-observability/monitoring"
if kube -n observability get secret grafana-admin >/dev/null 2>&1; then
  echo 'grafana-admin Secret already exists; refusing to replace it.' >&2; exit 1
fi
if [[ -e "$secret_dir" ]]; then
  echo "Credentials directory already exists: $secret_dir" >&2; exit 1
fi
umask 077
mkdir -p "$(dirname "$secret_dir")"
mkdir -m 700 "$secret_dir"
printf '%s' admin > "$secret_dir/username"
openssl rand -hex 24 | tr -d '\n' > "$secret_dir/password"
kube -n observability create secret generic grafana-admin \
  --from-file=username="$secret_dir/username" --from-file=password="$secret_dir/password"
echo "Credentials saved outside Git: $secret_dir"
