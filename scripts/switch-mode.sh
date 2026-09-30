#!/usr/bin/env bash
# Flip the ALB balancing mode between IN_FLIGHT and RATE (the A/B toggle), then wait for
# the GFEs to reprogram before you launch the next run.
#
#   ./scripts/switch-mode.sh inflight
#   ./scripts/switch-mode.sh rate
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:?usage: switch-mode.sh {inflight|rate}}"
[ -f "k8s/backendpolicy-${MODE}.yaml" ] || { echo "unknown mode: $MODE"; exit 1; }

kubectl apply -f "k8s/backendpolicy-${MODE}.yaml"
echo ">> applied $MODE; current policy:"
kubectl get gcpbackendpolicy inflight-backend-policy \
  -o jsonpath='   balancingMode={.spec.default.balancingMode}{"\n"}'
echo ">> waiting ~120s for GFEs to reprogram before the next run..."
sleep 120
echo ">> done -- reprogrammed. Run: ./scripts/run.sh <profile>"
