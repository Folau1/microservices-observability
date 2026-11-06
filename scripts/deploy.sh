#!/usr/bin/env bash
# Changes the cluster. Run only after explicit authorization and image import.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/kube.sh
kube get nodes
kube apply -f kubernetes/base/namespace.yaml
kube auth can-i get secrets -n microservices --quiet || { echo 'Cannot read Secrets in namespace microservices.' >&2; exit 1; }
if kube -n microservices get secret microservices-secrets >/dev/null 2>&1; then
  echo 'Existing microservices-secrets retained.'
else
  bash scripts/create-secrets.sh
fi
kube apply -k kubernetes/base
echo 'Resources submitted. Check rollouts with: bash scripts/check-cluster.sh'
