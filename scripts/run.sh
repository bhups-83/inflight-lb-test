#!/usr/bin/env bash
# Run one load profile against the ALB and save the log under results/.
#
#   ./scripts/run.sh <profile> [label]
#
# <profile> is a file in profiles/ (a set of env-var assignments). [label] tags the run
# and the output file (default: "<profile>-<balancingmode>"). The ALB IP is read from
# .gw_ip (written by setup.sh) or the ALB_URL env var.
#
# Requires: python3 with `pip install websockets==12.0`.
set -euo pipefail
cd "$(dirname "$0")/.."

PROFILE="${1:?usage: run.sh <profile> [label]   (profiles: $(ls profiles/ 2>/dev/null | tr '\n' ' '))}"
[ -f "profiles/${PROFILE}" ] || { echo "no such profile: profiles/${PROFILE}"; exit 1; }

if [ -z "${ALB_URL:-}" ]; then
  [ -f .gw_ip ] || { echo "no .gw_ip and no ALB_URL set; run setup.sh first"; exit 1; }
  export ALB_URL="ws://$(cat .gw_ip):80/"
fi

MODE=$(kubectl get gcpbackendpolicy inflight-backend-policy \
  -o jsonpath='{.spec.default.balancingMode}' 2>/dev/null || echo unknown)
LABEL="${2:-${PROFILE}-${MODE}}"

# shellcheck disable=SC1090
set -a; source "profiles/${PROFILE}"; set +a
export LABEL

mkdir -p results
OUT="results/$(date +%Y%m%d-%H%M%S)_${LABEL}.log"
echo ">> profile=$PROFILE mode=$MODE url=$ALB_URL -> $OUT"
python3 load/orchestrator.py | tee "$OUT"
echo ">> saved $OUT"
