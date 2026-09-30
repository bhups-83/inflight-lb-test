# GKE ALB balancing-mode test for long-lived WebSockets (IN_FLIGHT vs RATE)

A self-contained rig to measure **how a GCLB Global External ALB distributes long-lived
WebSocket connections across GKE pods**, comparing the `IN_FLIGHT` balancing mode against
the default `RATE` (round-robin-on-count) mode.

The motivating question: when a sudden burst of calls hits a WebSocket backend (e.g. a
morning traffic spike of ~1500 concurrent calls), does the load balancer spread them evenly
across pods, or does it overshoot/pin some pods — risking OOM on the exact pods that are
already hot? RATE-based balancing goes blind after the WS `101 Switching Protocols` upgrade
(the connection becomes an opaque TCP tunnel carrying ~0 further requests), so it can't see
how many calls a pod is *currently holding*. `IN_FLIGHT` tracks open proxied sessions per
endpoint and is the only native GCLB mode that accounts for concurrent long-lived
connections.

This rig uses a **dummy** WS backend (holds connections open, does no real work) so the only
thing under study is the LB's distribution behavior. It is registry/vendor-neutral: build
the image into your own registry and point the manifests at it.

---

## What's here

```
server/dummy_ws_server.py    dummy WS backend: holds each conn for a client-assigned duration
server/Dockerfile            public base (python:3.12-slim) + websockets==12.0
load/orchestrator.py         load client: drives N concurrent conns, measures per-pod spread
k8s/deployment.yaml          6 tiny dummy pods (image templated as __IMAGE__)
k8s/service.yaml             ClusterIP + container-native (NEG) annotation
k8s/gateway.yaml             Gateway (gke-l7-global-external-managed) + HTTPRoute
k8s/backendpolicy-inflight.yaml   GCPBackendPolicy: balancingMode IN_FLIGHT (+ trafficDuration LONG)
k8s/backendpolicy-rate.yaml       GCPBackendPolicy: balancingMode RATE  (the A/B baseline)
profiles/                    traffic profiles: steady, burst-peak, burst-abrupt, long-heavy, wave
scripts/setup.sh             build+push image, deploy rig, provision ALB
scripts/switch-mode.sh       flip IN_FLIGHT <-> RATE and wait for GFE reprogram
scripts/run.sh               run one profile, save log under results/
scripts/teardown.sh          delete everything (matched by label experiment=inflight)
```

## Prerequisites

- A GKE cluster with the Gateway API enabled and the `gke-l7-global-external-managed`
  GatewayClass available. `kubectl` pointed at it.
- `docker` + push access to a container registry the cluster can pull from (Artifact Registry).
- Python 3.9+ on the load-driving host, with `pip install websockets==12.0`.
- Egress from the load host to the ALB's public IP on port 80.

## Run it

```bash
# 1. build image, deploy rig, provision the ALB (defaults to IN_FLIGHT)
IMAGE=<region>-docker.pkg.dev/<project>/<repo>/inflight-dummy:v1 ./scripts/setup.sh
#    -> prints the Gateway public IP, saved to ./.gw_ip
#    (give the backend NEG 1-2 min to pass health checks before the first run)

# 2. run a profile under IN_FLIGHT
./scripts/run.sh burst-peak

# 3. flip to RATE and run the same profile
./scripts/switch-mode.sh rate
./scripts/run.sh burst-peak

# 4. sweep other profiles (each ~12-15 min)
./scripts/run.sh long-heavy
./scripts/run.sh burst-abrupt
# ...flip modes and repeat to A/B each one

# 5. tear it all down (the public ALB bills while it exists)
./scripts/teardown.sh
```

Every knob is an env var (see `load/orchestrator.py` header); the `profiles/` files just
set them. Override ad hoc: `CONC=300 RUN_S=300 ARRIVAL=burst ./scripts/run.sh burst-peak`.

## Reading the output

Each run prints a per-pod snapshot every 10s and a summary:

- `steady_avg_live_per_pod` / `CV` — coefficient of variation of **live** connections per
  pod once settled (t>=60s). **Low CV = even spread; high CV = skew/pinning.** This is the
  headline metric.
- `burst_overshoot` — the worst single-pod peak in the first 30s vs the steady per-pod mean,
  as a multiple (e.g. `2.0x`). This is what matters for a sudden spike: how far over its fair
  share can one pod get pushed before the LB corrects.
- `opened_per_pod` — total connections opened per pod. **RATE will look even here even when
  it's skewed on live count** — round-robin balances the *count of new connections*, not how
  many each pod is *currently holding*. Don't judge RATE by this line; judge it by live CV.

The pods also log their own `LIVE pod=<name> live=<n>` every 5s (`kubectl logs`), so you can
cross-check the distribution from the server side.

## Traffic profiles

| profile        | arrival | mix (long/med/short) | abrupt drops | what it stresses |
|----------------|---------|----------------------|--------------|------------------|
| `steady`       | ramp    | 25/35/40             | 0%           | settled distribution, no burst effect |
| `burst-peak`   | burst   | 25/35/40             | 0%           | the sudden spike (all at once) |
| `burst-abrupt` | burst   | 25/35/40             | 40%          | spike + ungraceful hangups (in-flight accounting recovery) |
| `long-heavy`   | burst   | 60/25/15             | 10%          | few pods hold most long calls (skew stress) |
| `wave`         | wave    | 25/35/40             | 15%          | oscillating load / autoscale churn |

To exercise a **scale event** (the case where IN_FLIGHT should separate hardest from RATE),
start a long run, then mid-run `kubectl scale deploy/inflight-dummy --replicas=9` and watch
whether new empty pods fill quickly (IN_FLIGHT floods them; RATE trickles 1/N).

---

## Reproducing each test

All runs are IN_FLIGHT unless noted. `ALB=ws://$(cat .gw_ip):80/`.

**1. A/B (IN_FLIGHT vs RATE), even fleet.** The headline comparison.
```bash
./scripts/run.sh burst-peak                       # IN_FLIGHT
./scripts/switch-mode.sh rate && ./scripts/run.sh burst-peak   # RATE
```

**2. Pre-skew (one pod pre-loaded, then a burst).** Concentrate long-lived connections on one
pod via a scale trick (the LB can't be told which pod to hit), then add churn.
```bash
kubectl scale deploy/inflight-dummy --replicas=1        # sole pod = the anchor
CONC=40 RUN_S=1800 ARRIVAL=burst LONG_PCT=100 LONG_MIN=1800 LONG_MAX=1800 \
  ALB_URL=$ALB python3 load/orchestrator.py &          # 40 long anchors land on it
kubectl scale deploy/inflight-dummy --replicas=6        # 5 fresh empty pods join
CONC=110 RUN_S=480 ARRIVAL=burst LONG_PCT=0 MED_PCT=0 SHORT_MIN=5 SHORT_MAX=20 \
  ALB_URL=$ALB python3 load/orchestrator.py            # churn; watch per-pod via kubectl logs
```
Add `ACTIVE_AUDIO=1` to both the deployment (`kubectl set env deploy/inflight-dummy ACTIVE_AUDIO=1`)
and the client env for the active-audio variant; bump pod CPU first (`kubectl set resources
deploy/inflight-dummy --requests=cpu=600m,memory=256Mi --limits=cpu=600m,memory=256Mi`).

**3. Role-based (one pod holds long, five hold short) — the clean load-awareness test.** No scale
trick: the *pod* decides its hold time. Deploy 5 short + 1 long (both in the Service), then just
maintain concurrency; the LB places every connection.
```bash
kubectl set env deploy/inflight-dummy SERVER_DECIDES_DURATION=1 HOLD_SHORT_MIN=5 HOLD_SHORT_MAX=20
kubectl scale deploy/inflight-dummy --replicas=5
IMAGE=<your-registry>/inflight-dummy:v1 envsubst < k8s/deployment-long.yaml | kubectl apply -f -  # 1 long pod
CONC=150 RUN_S=720 ARRIVAL=burst ALB_URL=$ALB python3 load/orchestrator.py
# watch the long pod accumulate:  kubectl logs -f <inflight-dummy-long-pod> | grep LIVE
```

`results/example-runs/` holds the sampler logs behind the numbers below.

## Findings (6 pods, 150 concurrent, 12-min runs, burst arrival, mix 25/35/40)

Staging A/B, same rig/profile, only `balancingMode` flipped:

| mode      | steady live-CV | busiest vs idlest pod (avg live) | opened_per_pod spread | burst peak (first ~40s) |
|-----------|----------------|----------------------------------|-----------------------|--------------------------|
| **IN_FLIGHT** | **0.119** | 29.6 vs 21.0  (**1.4x**) | 131 – 157  (even) | one pod hit 59 vs 30 cap (~2x), converged to even by ~t=90s |
| **RATE**      | **0.302** | **40.4 vs 17.8  (2.3x)** | **101 – 229  (skewed)** | burst pod pinned ~40-57 for the WHOLE run, never drained |

**RATE is ~2.5x worse on live-CV and the skew is permanent, not transient.** The pod that
caught the opening burst (`4wmlh`) stayed at ~2.3x the least-loaded pod's live count for the
entire 12-min run, and RATE even kept *choosing* it for new connections (229 opened vs 101 on
the idlest pod). This is the key result: **round-robin/RATE is even on neither live count nor
opened count for long-lived WS** — after the WS `101` upgrade each connection is ~0 RPS, so
RATE's rate accounting has almost no signal and cannot tell a loaded pod from an idle one.
IN_FLIGHT counts live sessions and actively rebalances (busiest pod only 1.4x the idlest).

Interpretation:

- **"Won't round-robin be just as even?"** — No, not for this workload. It would tie IN_FLIGHT
  only if all calls were the same length. With heterogeneous durations + churn, RATE has no
  feedback and the burst-induced skew never corrects (CV 0.302 vs 0.119).
- **Burst (the 8AM concern):** IN_FLIGHT does **not** hard-cap a simultaneous burst — the
  GFE's in-flight accounting lags a wall of near-simultaneous connects, so a pod can
  transiently take ~2x `maxInFlightRequestsPerEndpoint`, then self-corrects over ~60-90s. For a
  backend that OOMs near its per-pod ceiling, size for that transient: set
  `maxInFlightRequestsPerEndpoint` with headroom and keep enough replicas that 2x fair-share
  survives. Under RATE the overload is not transient — it persists.
- IN_FLIGHT's advantage over RATE grows with duration heterogeneity and during scale-up events.

### Pre-skew test — can IN_FLIGHT *correct* an already-hot pod? (IN_FLIGHT only)

Setup: scale to 1 pod, pin **40 long-lived** connections on it (all land there), scale back to
6 (five fresh empty pods), then add **110 short-lived churn** (5–20s) — total ~150. Question:
does IN_FLIGHT drain the hot pod back toward fair share, and how fast?

Result over the full 8-min churn window (per-pod *total* live, from pod logs):

| | anchor pod A | other 5 pods (each) | A / others | CV(all 6) |
|---|---|---|---|---|
| whole run (t=0→480s) | **~55** (40 anchors + ~15 churn) | **~18** | **~3.1x, sustained** | ~0.55–0.77, never drops |

**IN_FLIGHT did NOT correct the pre-existing skew.** It spread the churn ~evenly across all six
pods *including* the already-hot one (A got ~15 of the churn — only marginally below the ~18
even share, i.e. essentially no protection), so A stayed ~3x the others for the entire run and
the system never converged. The five fresh pods balanced *among themselves* within ~10–30s, but
the hot pod only "resolves" when its long-lived connections finally end (then A drops to 0 and
rejoins) — not because IN_FLIGHT rebalanced it.

Reconciling with the A/B above: IN_FLIGHT applies a **weak, weighted least-loaded bias** — strong
enough to keep an *initially-even* fleet even and stop RATE-style drift (CV 0.12 vs 0.30), but
**too weak to relieve a pod that is already heavily pre-loaded**. And it fundamentally cannot
migrate existing connections. So for the 8AM concern: if a burst hits a fleet that is *already
unevenly loaded* (some pods holding long calls), IN_FLIGHT will add roughly even share to
everyone and will **not** rescue the hot pods — they can be pushed further over their ceiling.

### Pre-skew, ACTIVE-AUDIO variant — resolves the idle caveat

Re-ran the exact pre-skew above but with `ACTIVE_AUDIO=1` on both sides: every connection
(anchors + churn) streams a frame each way every 150ms for its whole life, mimicking live
bidirectional call audio (rules out the "idle connections may not be counted" caveat). Pods
bumped to 600m CPU / 256Mi so streaming doesn't starve them.

Result: **identical conclusion.** The anchor pod held ~50–59 total (40 anchors + ~14 churn) for
the entire run — it got ~13.6 of the churn, essentially its even share (fleet mean ~13), i.e.
**still no protection despite active audio**. CV(all) stayed ~0.8–1.0 the whole run; A remained
~4× the other pods and never converged.

So the idle-vs-active distinction does **not** change the behavior: IN_FLIGHT's in-flight
accounting does not cause it to withhold new connections from a pod already loaded with
long-lived connections, whether those connections are idle or actively streaming. The
"IN_FLIGHT won't relieve an already-hot pod" conclusion holds for the real (active-audio) call
path.

Secondary observation: active audio is **much heavier** on the backends — at the original 250m
CPU the anchor pod's readiness probe flapped under 40 streaming connections and it dropped out
of the NEG; even at 600m CPU the fleet showed some pod churn and a few `502`s
(`InvalidStatus`) during the burst. Sizing for active streaming needs real CPU headroom, not the
"bare-minimum" the idle dummy got away with.

### Role-based test — the clean one: is IN_FLIGHT actually load-aware? (no scale tricks)

The pre-skew tests above created the hot pod via a scale event, which leaves open "maybe the
scale-up reset the GFE's in-flight counters." This test removes that entirely: **stable 6-pod
fleet, all traffic routed normally through the ALB**, but the *pod itself* decides how long to
hold each connection by its role — **one "long" pod holds every connection 300–600s, the other
five hold 5–20s** (server-side `SERVER_DECIDES_DURATION=1` + `LONG_HOLD=1` on one deployment
sharing the Service). The client just maintains ~150 concurrent; the LB places every connection.

If IN_FLIGHT were load-aware it would stop feeding the long pod once it passed the others (~25).
Instead (per-pod live over the run):

| t | long pod | 5 short pods (sum) | long pod % of all |
|---|---|---|---|
| 20s | 54 | 92 | 37% |
| 60s | 96 | 53 | 64% |
| 120s | 112 | 37 | 75% |
| ~300s (plateau) | **~124** | ~24 | **84%** |
| steady avg | **109.9** | 2.7–15.3 each | ~73% |

**The long pod ran away to ~110–124 of 150 connections (73–84%), starving several short pods to
0–2. CV = 1.55.** IN_FLIGHT kept handing the long pod its ~even share of new connections the
whole time, with no corrective steering as it climbed past 25, 50, 100. This is round-robin-style
connection spreading, **not** live-load balancing.

Note the policy had **`maxInFlightRequestsPerEndpoint = 30`** (the per-pod "connections per
instance" soft cap, applied across all 3 zonal NEGs). The long pod reached **~124 — over 4× that
cap — and stayed there.** The cap was never enforced. That is the clearest single signal that the
GFE is **not counting established WS connections** against its in-flight metric: if it were, it
would have stopped at ~30. (The first burst test already showed 59 vs the 30 cap transiently;
here it's ~4× sustained.) So `maxInFlightRequestsPerEndpoint` does nothing for long-lived WS —
neither capping the pod nor feeding the balancer a signal.

**Verdict:** for long-lived WebSockets, IN_FLIGHT does **not** provide effective load-aware
steering. The most likely reason: once a WS is upgraded (`101`), the GFE no longer counts it as
an "in-flight request", so its in-flight metric is ~blind to established call connections — the
same blind spot RATE has after `101`, just with a more even default connection spread. It cannot
migrate existing connections and it will not divert new ones away from a pod that is accumulating
long calls. A pod that holds calls longer (or comes back empty after a restart, or catches a
burst) will pile up and stay piled up.

This is the empirical basis for keeping the Go tunnel's Redis least-loaded brain: it is the only
layer in this stack that actually steers by *live per-pod call count*. IN_FLIGHT is a modest
improvement over RATE for an evenly-loaded fleet, but it is not a substitute for the tunnel.

### What the ALB access logs show (and don't)

With `logging.enabled` (set in the backend policies here), GFE access logs land in Cloud
Logging (`resource.type="http_load_balancer"`); pull + summarize them with
`scripts/fetch-lb-logs.sh`. A logged run shows every WS connection as `status: 101`,
`statusDetails: backend_connection_closed_after_partial_response_sent` on clean close, and
`httpRequest.latency` = the full connection duration (our sample: p50 26.5s / p90 89.6s /
max 111s, matching the call-duration mix).

**Limitation:** GFE access logs record the backend *service*, not the individual NEG endpoint
(pod). So the LB logs confirm the *traffic shape* (counts, durations, status, any 5xx /
capacity `statusDetails`) but are **identical-looking for IN_FLIGHT and RATE** — they cannot
show the balancing-quality difference. Per-pod distribution must come from the load client
(pod name in the first frame) and the pods' own `LIVE pod=.. live=..` logs. Also: access-log
sampling is decided at connection *start*, and enabling logging takes a few minutes to
propagate to the GFEs, so enable it before the run, not during.
