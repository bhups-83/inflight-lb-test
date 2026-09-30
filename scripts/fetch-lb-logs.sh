#!/usr/bin/env bash
# Pull the ALB access logs for this experiment's backend service from Cloud Logging and
# summarize them. Requires logging enabled on the backend (the GCPBackendPolicy in this
# repo sets logging.enabled=true) and a few minutes of propagation lag.
#
#   PROJECT=<gcp-project> ./scripts/fetch-lb-logs.sh [freshness]
#
# NOTE ON PER-POD DISTRIBUTION: GFE access logs record the backend SERVICE, not the
# individual NEG endpoint (pod) that served each request -- so these logs give the LB's-eye
# view (request counts, connection DURATIONS via httpRequest.latency, status codes, and
# jsonPayload.statusDetails) but NOT per-pod counts. Per-pod distribution comes from the
# load client (pod name in the first frame) and the pods' own `LIVE pod=.. live=..` logs.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${PROJECT:?set PROJECT=<gcp-project>}"
FRESH="${1:-30m}"

# Resolve the backend service name created by the Gateway for our service.
BES=$(gcloud compute backend-services list --project="$PROJECT" \
  --format="value(name)" --filter="name~inflight-dummy-svc" 2>/dev/null | head -1)
[ -z "$BES" ] && { echo "no backend service matching inflight-dummy-svc found"; exit 1; }
echo ">> backend service: $BES  (freshness=$FRESH)"

FILTER="resource.type=\"http_load_balancer\" AND resource.labels.backend_service_name=\"$BES\""
OUT="results/lb-logs_$(date +%Y%m%d-%H%M%S).json"
mkdir -p results
gcloud logging read "$FILTER" --project="$PROJECT" --freshness="$FRESH" \
  --format=json > "$OUT"
N=$(python3 -c "import json,sys;print(len(json.load(open('$OUT'))))")
echo ">> pulled $N log entries -> $OUT"
[ "$N" = "0" ] && { echo ">> (none yet -- logs lag a few min, and WS entries appear at connection CLOSE)"; exit 0; }

python3 - "$OUT" <<'PY'
import json, sys, collections, statistics
rows = json.load(open(sys.argv[1]))
status = collections.Counter()
details = collections.Counter()
lat = []
for r in rows:
    h = r.get("httpRequest", {})
    status[h.get("status", "?")] += 1
    details[r.get("jsonPayload", {}).get("statusDetails", "?")] += 1
    l = h.get("latency", "")
    if l.endswith("s"):
        try: lat.append(float(l[:-1]))
        except ValueError: pass
print("status_codes =", dict(status))
print("statusDetails =", dict(details))
if lat:
    lat.sort()
    q = lambda p: lat[min(len(lat)-1, int(p*len(lat)))]
    print(f"conn_duration_s (LB latency): n={len(lat)} min={lat[0]:.1f} "
          f"p50={q(.5):.1f} p90={q(.9):.1f} max={lat[-1]:.1f} mean={statistics.mean(lat):.1f}")
PY
