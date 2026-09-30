#!/usr/bin/env bash
# Remove everything this experiment created (matched by the `experiment: inflight` label),
# including the public ALB. Run this when done -- the global forwarding rule / IP bills while it exists.
set -euo pipefail
cd "$(dirname "$0")/.."
echo ">> deleting all experiment=inflight resources"
kubectl delete gateway,httproute,gcpbackendpolicy,deploy,svc -l experiment=inflight --ignore-not-found
echo ">> done. Verify no leftover forwarding rules:  gcloud compute forwarding-rules list | grep -i inflight"
