#!/usr/bin/env bash
# Build + push the dummy image, deploy the rig, and provision the ALB.
#
#   IMAGE=<region>-docker.pkg.dev/<project>/<repo>/inflight-dummy:v1 ./scripts/setup.sh
#
# Requires: kubectl pointed at the target GKE cluster, docker, and push access to IMAGE's
# registry. Idempotent -- safe to re-run. Defaults balancing mode to IN_FLIGHT; flip with
# scripts/switch-mode.sh {inflight|rate}.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${IMAGE:?set IMAGE=<registry>/inflight-dummy:<tag>}"
MODE="${MODE:-inflight}"

echo ">> building + pushing $IMAGE"
docker build -t "$IMAGE" server/
docker push "$IMAGE"

echo ">> deploying dummy backend (image substituted)"
sed "s#__IMAGE__#${IMAGE}#g" k8s/deployment.yaml | kubectl apply -f -
kubectl apply -f k8s/service.yaml

echo ">> applying ALB (gateway + route) and balancing policy: $MODE"
kubectl apply -f k8s/gateway.yaml
kubectl apply -f "k8s/backendpolicy-${MODE}.yaml"

echo ">> waiting for dummy pods to be Ready"
kubectl rollout status deploy/inflight-dummy --timeout=120s

echo ">> waiting for the Gateway to get a public address (can take a few minutes)"
for i in $(seq 1 60); do
  ADDR=$(kubectl get gateway inflight-gw -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)
  [ -n "${ADDR:-}" ] && break
  sleep 10
done
[ -z "${ADDR:-}" ] && { echo "!! gateway has no address yet; check: kubectl describe gateway inflight-gw"; exit 1; }
echo "$ADDR" > .gw_ip
echo ">> Gateway IP: $ADDR  (saved to .gw_ip)"
echo ">> NOTE: the backend NEG may need another 1-2 min to pass health checks before traffic routes."
echo ">> next: ./scripts/run.sh <profile>   (profiles in profiles/)"
