#!/usr/bin/env bash
# Read-only inventory. Execute against the intended cluster only.
set -euo pipefail
source "$(dirname "$0")/kube.sh"
kube get nodes -o wide
kube -n microservices get deployment,pod,service,ingress,pvc -o wide
kube -n microservices get events --sort-by=.lastTimestamp | tail -30
kube -n microservices rollout status deployment/frontend --timeout=90s
kube -n microservices rollout status deployment/security --timeout=90s
kube -n microservices rollout status deployment/uploader --timeout=90s
kube -n microservices rollout status deployment/minio --timeout=180s
