#!/usr/bin/env bash
# Run on the K3s host as the tunnel user with temporary Kubernetes-admin access.
# Produces a limited kubeconfig and expiring token outside the repository.
set -euo pipefail
case "${KUBECTL_MODE:-kubectl}" in
  kubectl) kube() { command kubectl "$@"; } ;;
  k3s) kube() { sudo k3s kubectl "$@"; } ;;
  *) echo 'KUBECTL_MODE must be kubectl or k3s' >&2; exit 2 ;;
esac
state="${XDG_STATE_HOME:-$HOME/.local/state}/microservices-observability/monitoring/access"
umask 077
mkdir -p "$state"
chmod 700 "$state"
if [[ -e "$state/token" || -e "$state/kubeconfig" || -e "$state/ca.crt" ]]; then
  echo "Access files already exist: $state; refusing to replace them." >&2
  exit 1
fi
temp=$(mktemp "$state/token.XXXXXXXX")
trap 'rm -f -- "$temp"' EXIT
kube -n observability create token monitoring-portforward --duration=720h > "$temp"
test -s "$temp"
chmod 600 "$temp"
mv -f -- "$temp" "$state/token"
kube -n observability get configmap kube-root-ca.crt -o 'jsonpath={.data.ca\.crt}' > "$state/ca.crt"
test -s "$state/ca.crt"
chmod 600 "$state/ca.crt"
cat > "$state/kubeconfig" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: local-k3s
    cluster:
      certificate-authority: "$state/ca.crt"
      server: https://127.0.0.1:6443
contexts:
  - name: monitoring
    context:
      cluster: local-k3s
      user: monitoring-portforward
      namespace: observability
current-context: monitoring
users:
  - name: monitoring-portforward
    user:
      tokenFile: "$state/token"
EOF
chmod 600 "$state/kubeconfig"
if [[ "${KUBECTL_MODE:-kubectl}" == k3s ]]; then
  KUBECONFIG="$state/kubeconfig" k3s kubectl -n observability auth can-i create pods --subresource=portforward --quiet
else
  KUBECONFIG="$state/kubeconfig" kubectl -n observability auth can-i create pods --subresource=portforward --quiet
fi
echo "Limited kubeconfig saved outside Git: $state/kubeconfig"
python3 - "$state/token" <<'PY'
import base64, datetime, json, pathlib, sys
token = pathlib.Path(sys.argv[1]).read_text().strip()
payload = token.split('.')[1]
claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
print('Token expiry UTC:', datetime.datetime.fromtimestamp(claims['exp'], datetime.timezone.utc).isoformat())
PY
