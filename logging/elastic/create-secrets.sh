#!/usr/bin/env bash
# First installation only. Run as root on node2; never commit the output.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run as root on node2.' >&2; exit 1; }
[[ $(hostname) == node2 ]] || { echo 'Unexpected host.' >&2; exit 1; }
for cmd in openssl k3s findmnt mountpoint stat find; do command -v "$cmd" >/dev/null || exit 1; done
base=/var/lib/codex-elasticsearch
data=$base/data
secrets=/var/lib/codex-logging-secrets
mountpoint -q "$base" || { echo 'Dedicated filesystem is not mounted.' >&2; exit 1; }
[[ $(findmnt -rn -M "$base" -o FSTYPE) == ext4 ]] || { echo 'Expected dedicated ext4 mount.' >&2; exit 1; }
[[ -d "$data" && ! -L "$data" ]] || { echo 'Data directory missing or a symlink.' >&2; exit 1; }
[[ $(stat -c %d "$base") == "$(stat -c %d "$data")" ]] || { echo 'Data directory is on another filesystem.' >&2; exit 1; }
[[ -z $(find "$data" -mindepth 1 -maxdepth 1 -print -quit) ]] || { echo 'Data directory is not empty: do not reset credentials.' >&2; exit 1; }
[[ ! -e "$secrets" ]] || { echo 'Secret directory already exists; do not overwrite.' >&2; exit 1; }
for name in elasticsearch-tls elasticsearch-http-ca elasticsearch-admin; do
  if k3s kubectl -n logging get secret "$name" >/dev/null 2>&1; then
    echo 'An Elasticsearch secret already exists; do not overwrite.' >&2; exit 1
  fi
done
umask 077
mkdir -m 0700 "$secrets"
echo 'Creating persistent TLS material and bootstrap password in a root-only host directory.'
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$secrets/ca.key" -out "$secrets/ca.crt" -days 3650 -subj '/CN=microservices-logging-ca' >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -keyout "$secrets/tls.key" -out "$secrets/tls.csr" -subj '/CN=elasticsearch.logging.svc' >/dev/null 2>&1
cat > "$secrets/extensions.cnf" <<'EOF'
subjectAltName=DNS:elasticsearch,DNS:elasticsearch.logging,DNS:elasticsearch.logging.svc,DNS:elasticsearch.logging.svc.cluster.local,DNS:localhost,IP:127.0.0.1
extendedKeyUsage=serverAuth,clientAuth
keyUsage=digitalSignature,keyEncipherment
EOF
openssl x509 -req -in "$secrets/tls.csr" -CA "$secrets/ca.crt" -CAkey "$secrets/ca.key" -CAcreateserial -out "$secrets/tls.crt" -days 825 -sha256 -extfile "$secrets/extensions.cnf" >/dev/null 2>&1
openssl rand -hex 32 | tr -d '\n' > "$secrets/password"
printf 'user = "elastic:%s"\ncacert = "/usr/share/elasticsearch/config/certs/ca.crt"\n' "$(cat "$secrets/password")" > "$secrets/curl.conf"
if ! k3s kubectl get namespace logging >/dev/null 2>&1; then k3s kubectl create namespace logging; fi
k3s kubectl -n logging create secret generic elasticsearch-http-ca --from-file=ca.crt="$secrets/ca.crt"
k3s kubectl -n logging create secret generic elasticsearch-tls --from-file=ca.crt="$secrets/ca.crt" --from-file=tls.crt="$secrets/tls.crt" --from-file=tls.key="$secrets/tls.key"
k3s kubectl -n logging create secret generic elasticsearch-admin --from-file=password="$secrets/password" --from-file=curl.conf="$secrets/curl.conf"
echo 'Secrets created. Back up the root-only directory securely before relying on this service.'
