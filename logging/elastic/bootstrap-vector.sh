#!/usr/bin/env bash
# Run as root after ES is Ready; never print credentials or replace secrets.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
cd "$(dirname "$0")"
secrets=/var/lib/codex-logging-secrets
[[ -d "$secrets" && -f "$secrets/password" ]] || { echo 'ES bootstrap secrets missing.' >&2; exit 1; }
[[ ! -e "$secrets/vector-password" ]] || { echo 'Vector password already exists; do not rotate automatically.' >&2; exit 1; }
if k3s kubectl -n logging get secret vector-auth >/dev/null 2>&1; then echo 'vector-auth already exists; refusing to overwrite.' >&2; exit 1; fi
[[ -f ../ilm-policy.json && -f ../index-template.json ]] || { echo 'Index configuration missing.' >&2; exit 1; }
api() {
  local method=$1 path=$2
  k3s kubectl -n logging exec -i elasticsearch-0 -c elasticsearch -- curl --config /usr/share/elasticsearch/config/auth/curl.conf --fail-with-body --silent --show-error --max-time 30 -X "$method" -H 'Content-Type: application/json' --data-binary @- "https://127.0.0.1:9200$path"
  printf '\n'
}
api PUT /_ilm/policy/microservices-3d < ../ilm-policy.json
api PUT /_index_template/microservices-logs < ../index-template.json
printf '%s' '{"cluster":["monitor"],"indices":[{"names":["logs-microservices-default"],"privileges":["auto_configure","create_doc"]}]}' | api PUT /_security/role/microservices_vector
umask 077
printf '%s' vector_writer > "$secrets/vector-username"
openssl rand -hex 32 | tr -d '\n' > "$secrets/vector-password"
python3 - "$secrets/vector-password" <<'PY' | api PUT /_security/user/vector_writer
import json, pathlib, sys
print(json.dumps({'password': pathlib.Path(sys.argv[1]).read_text(), 'roles': ['microservices_vector'], 'full_name': 'Microservices log writer'}))
PY
k3s kubectl -n logging create secret generic vector-auth --from-file=username="$secrets/vector-username" --from-file=password="$secrets/vector-password"
echo 'Vector role, user and Secret configured; verify with a real write and restricted-access test.'
