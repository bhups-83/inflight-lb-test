#!/usr/bin/env python3
"""Load orchestrator for the GKE ALB balancing-mode experiment.

Drives a configurable population of WebSocket connections at the ALB and measures how
evenly they land across the backend pods. Each connection:
  1. connects to the ALB,
  2. receives (first frame) the name of the pod it landed on,
  3. tells the server how long to hold it (an assigned "call duration"), and
  4. either waits for the server to close it, or -- for a configurable fraction --
     hangs up ABRUPTLY early (mimicking dropped calls / client RSTs).

It prints a per-pod live-connection snapshot every 10s and, at the end, a distribution
summary: the coefficient of variation (CV) of steady-state live connections per pod
(low = even spread, high = skew/pinning) and the *burst overshoot* (the worst single-pod
peak during the opening burst vs. the steady per-pod mean -- the number that matters for a
sudden 8AM-style spike).

All knobs are env vars so the same script covers every traffic profile (see profiles/ and
scripts/run.sh). Requires: pip install websockets==12.0

  ALB_URL         ws://<gateway-ip>:80/     target ALB WebSocket URL   (required)
  LABEL           run                       tag for log lines + summary
  CONC            150                       target concurrent connections
  RUN_S           720                       run duration (seconds)
  ARRIVAL         burst                     burst | ramp | wave  (how connections arrive)
  RAMP_RATE       50                        new conns/sec for ARRIVAL=ramp
  WAVE_PERIOD     60                        seconds/cycle for ARRIVAL=wave (CONC..CONC/3)
  LONG_PCT        25                        % long-lived calls   (skew source)
  MED_PCT         35                        % medium calls       (short = the remainder)
  LONG_MIN/MAX    300 / RUN_S               long-call duration range (s)
  MED_MIN/MAX     30 / 120                  medium-call duration range (s)
  SHORT_MIN/MAX   5 / 30                    short-call duration range (s)
  ABRUPT_PCT      0                         % of calls that drop ABRUPTLY (hard close) early
  ABRUPT_MAX_S    20                        abrupt drops happen at a random t in [1, this]
  ACTIVE_AUDIO    0                         1 = stream frames both ways for the call's duration
  AUDIO_INTERVAL_MS 150                     frame cadence per direction when ACTIVE_AUDIO=1
  AUDIO_FRAME_BYTES 320                     frame size when ACTIVE_AUDIO=1
"""
import asyncio
import collections
import os
import random
import statistics
import time

import websockets


def _f(name, default):
    return float(os.getenv(name, default))


def _i(name, default):
    return int(float(os.getenv(name, default)))


ALB_URL = os.getenv("ALB_URL") or os.getenv("URI")
if not ALB_URL:
    raise SystemExit("set ALB_URL=ws://<gateway-ip>:80/")
LABEL = os.getenv("LABEL", "run")
CONC = _i("CONC", 150)
RUN_S = _i("RUN_S", 720)
ARRIVAL = os.getenv("ARRIVAL", "burst").lower()
RAMP_RATE = _f("RAMP_RATE", 50)
WAVE_PERIOD = _f("WAVE_PERIOD", 60)
LONG_PCT = _f("LONG_PCT", 25)
MED_PCT = _f("MED_PCT", 35)
LONG_MIN, LONG_MAX = _f("LONG_MIN", 300), _f("LONG_MAX", RUN_S)
MED_MIN, MED_MAX = _f("MED_MIN", 30), _f("MED_MAX", 120)
SHORT_MIN, SHORT_MAX = _f("SHORT_MIN", 5), _f("SHORT_MAX", 30)
ABRUPT_PCT = _f("ABRUPT_PCT", 0)
ABRUPT_MAX_S = _f("ABRUPT_MAX_S", 20)
ACTIVE_AUDIO = os.getenv("ACTIVE_AUDIO") == "1"
AUDIO_MS = _f("AUDIO_INTERVAL_MS", 150)
FRAME = b"\x00" * _i("AUDIO_FRAME_BYTES", 320)

live = collections.Counter()    # pod -> currently-live connections (this client's view)
opened = collections.Counter()  # pod -> total connections opened over the run
errors = collections.Counter()


def pick_dur():
    r = random.random() * 100
    if r < LONG_PCT:
        return random.uniform(LONG_MIN, max(LONG_MIN, LONG_MAX))   # long-lived (skew source)
    if r < LONG_PCT + MED_PCT:
        return random.uniform(MED_MIN, MED_MAX)                    # medium
    return random.uniform(SHORT_MIN, SHORT_MAX)                    # short churn


async def _client_stream(ws, dur):
    # Bidirectional traffic for ~dur seconds: send a frame every AUDIO_MS, drain incoming.
    async def send_loop():
        while True:
            await ws.send(FRAME)
            await asyncio.sleep(AUDIO_MS / 1000)

    async def recv_loop():
        while True:
            await ws.recv()

    tasks = [asyncio.ensure_future(send_loop()), asyncio.ensure_future(recv_loop())]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=dur)
    except Exception:
        pass
    finally:
        for t in tasks:
            t.cancel()


async def one():
    pod = None
    abrupt = random.random() * 100 < ABRUPT_PCT
    try:
        async with websockets.connect(ALB_URL, open_timeout=15, ping_interval=20,
                                      ping_timeout=60, max_size=2 ** 16) as ws:
            pod = await asyncio.wait_for(ws.recv(), timeout=15)
            live[pod] += 1
            opened[pod] += 1
            d = pick_dur()
            await ws.send(str(d))
            if abrupt:
                # Drop hard partway through, without a clean close handshake.
                await asyncio.sleep(random.uniform(1, ABRUPT_MAX_S))
                await ws.transport.close()  # abrupt: kill the socket, no WS close frame
            elif ACTIVE_AUDIO:
                await _client_stream(ws, d)  # stream frames both ways until the call ends
            else:
                try:
                    while True:
                        await ws.recv()  # blocks until the server closes at the assigned duration
                except Exception:
                    pass
    except Exception as e:
        errors[type(e).__name__] += 1
    finally:
        if pod is not None:
            live[pod] -= 1


def target_now(elapsed):
    """Concurrency setpoint at a given elapsed time, per the arrival profile."""
    if ARRIVAL == "ramp":
        return min(CONC, int(RAMP_RATE * elapsed))
    if ARRIVAL == "wave":
        import math
        # oscillate between CONC and CONC/3
        frac = (math.sin(2 * math.pi * elapsed / WAVE_PERIOD) + 1) / 2  # 0..1
        return int(CONC / 3 + frac * (CONC - CONC / 3))
    return CONC  # burst: fill to CONC immediately and hold


async def run():
    print(f"[{LABEL}] cfg url={ALB_URL} conc={CONC} run={RUN_S}s arrival={ARRIVAL} "
          f"mix(long/med/short)={LONG_PCT:.0f}/{MED_PCT:.0f}/{100 - LONG_PCT - MED_PCT:.0f} "
          f"abrupt={ABRUPT_PCT:.0f}%", flush=True)
    tasks = set()
    snaps = []            # (elapsed, {pod: live})
    t0 = time.time()
    end = t0 + RUN_S
    nextsnap = t0 + 10
    while time.time() < end:
        elapsed = time.time() - t0
        tasks = {t for t in tasks if not t.done()}
        want = target_now(elapsed)
        for _ in range(max(0, want - len(tasks))):
            tasks.add(asyncio.create_task(one()))
        if time.time() >= nextsnap:
            snap = {p: c for p, c in live.items() if c}
            print(f"[{LABEL}] t={int(elapsed)}s concurrent={sum(snap.values())} "
                  f"per_pod={sorted(snap.items())}", flush=True)
            snaps.append((int(elapsed), dict(snap)))
            nextsnap += 10
        await asyncio.sleep(0.5)

    print(f"=== SUMMARY [{LABEL}] ===", flush=True)
    print("opened_per_pod=", dict(sorted(opened.items())), flush=True)
    print("errors=", dict(errors), flush=True)
    pods = sorted(opened)

    # Burst overshoot: worst single-pod peak in the first 30s vs. the steady per-pod mean.
    burst = [s for (e, s) in snaps if e <= 30]
    steady = [s for (e, s) in snaps if e >= 60]  # drop the first 60s of ramp/settling
    if steady and pods:
        avg = {p: round(statistics.mean(s.get(p, 0) for s in steady), 1) for p in pods}
        vals = list(avg.values())
        m = sum(vals) / len(vals)
        cv = (statistics.pstdev(vals) / m) if m else 0
        print("steady_avg_live_per_pod=", avg, flush=True)
        print(f"steady: pods={len(pods)} mean={m:.1f} min={min(vals)} max={max(vals)} "
              f"CV={cv:.3f}  (low CV = even; high = skew/pinning)", flush=True)
        if burst:
            peak = max((c for s in burst for c in s.values()), default=0)
            print(f"burst_overshoot: worst_pod_peak_first30s={peak} "
                  f"vs steady_pod_mean={m:.1f}  ->  {peak / m:.2f}x" if m else "", flush=True)


if __name__ == "__main__":
    asyncio.run(run())
