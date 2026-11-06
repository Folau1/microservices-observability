#!/usr/bin/env bash
# Generate demo credentials and retain a local recovery copy outside Git.
set -euo pipefail
source "$(dirname "$0")/kube.sh"
command -v openssl >/dev/null
secret_dir="${XDG_STATE_HOME:-$HOME/.local/state}/microservices-observability/secrets"
kube auth can-i get secrets -n microservices --quiet || { echo 'Cannot read Kubernetes Secrets.' >&2; exit 1; }
kube auth can-i create secrets -n microservices --quiet || { echo 'Cannot create Kubernetes Secrets.' >&2; exit 1; }
if kube -n microservices get secret microservices-secrets >/dev/null 2>&1; then
  echo 'Secret already exists; refusing to replace it.' >&2
  exit 1
fi
if [[ -e "$secret_dir" ]]; then
  echo "Credentials directory already exists: $secret_dir. Refusing to replace it." >&2
  exit 1
fi
umask 077
mkdir -p "$(dirname "$secret_dir")"
mkdir -m 700 "$secret_dir"
printf '%s' demo > "$secret_dir/DEMO_USERNAME"
openssl rand -hex 16 | tr -d '\n' > "$secret_dir/DEMO_PASSWORD"
openssl rand -hex 32 | tr -d '\n' > "$secret_dir/TOKEN_SECRET"
openssl rand -hex 16 | tr -d '\n' > "$secret_dir/MINIO_ROOT_USER"
openssl rand -hex 32 | tr -d '\n' > "$secret_dir/MINIO_ROOT_PASSWORD"
kube -n microservices create secret generic microservices-secrets --from-file="$secret_dir"
echo "Credentials saved outside the repository: $secret_dir"
echo 'Keep this directory private and back it up securely before moving the PVC.'
