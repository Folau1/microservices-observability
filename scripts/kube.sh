#!/usr/bin/env bash
# Source this helper; KUBECTL_MODE=k3s uses the bundled client via sudo.
kube() {
  case "${KUBECTL_MODE:-kubectl}" in
    kubectl) command kubectl "$@" ;;
    k3s) sudo k3s kubectl "$@" ;;
    *) echo 'KUBECTL_MODE must be kubectl or k3s' >&2; return 2 ;;
  esac
}
