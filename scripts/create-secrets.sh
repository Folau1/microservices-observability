#!/usr/bin/env bash
# Run only against the intended cluster after server changes are authorized.
set -euo pipefail
source "$(dirname "$0")/kube.sh"
command -v openssl >/dev/null
kube auth can-i get secrets -n microservices --quiet || { echo 'Cannot read Secrets in namespace microservices.' >&2; exit 1; }
kube auth can-i create secrets -n microservices --quiet || { echo 'Cannot create Secrets in namespace microservices.' >&2; exit 1; }
if kube -n microservices get secret microservices-secrets >/dev/null 2>&1; then
  echo 'Secret already exists; refusing to replace credentials and invalidate stored objects.' >&2
  exit 1
fi
umask 077
secret_dir=$(mktemp -d)
trap 'rm -rf -- "$secret_dir"' EXIT
read -r -p 'Demo username: ' demo_username
read -r -s -p 'Demo password: ' demo_password; echo
read -r -s -p 'Repeat demo password: ' demo_password_check; echo
if [[ -z "$demo_username" || -z "$demo_password" || "$demo_password" != "$demo_password_check" ]]; then
  echo 'Username/password empty or passwords differ.' >&2
  exit 1
fi
printf '%s' "$demo_username" > "$secret_dir/DEMO_USERNAME"
printf '%s' "$demo_password" > "$secret_dir/DEMO_PASSWORD"
openssl rand -hex 32 | tr -d '\n' > "$secret_dir/TOKEN_SECRET"
openssl rand -hex 16 | tr -d '\n' > "$secret_dir/MINIO_ROOT_USER"
openssl rand -hex 32 | tr -d '\n' > "$secret_dir/MINIO_ROOT_PASSWORD"
unset demo_password demo_password_check
kube -n microservices create secret generic microservices-secrets --from-file="$secret_dir"
echo 'Secret created. Demo credentials are the username and password entered above.'
echo 'Back up the MinIO credentials securely before recreating the Secret or moving the PVC.'
