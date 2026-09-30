#!/usr/bin/env python3
"""Dummy WebSocket backend for the GKE ALB balancing-mode experiment.

Mimics a telephony/TTS backend pod: accepts a WS connection, tells the client which
pod it landed on (first frame), then holds the connection open for a client-assigned
duration (mimicking a call), then closes. Does no real work, so many connections fit
in tiny CPU/RAM and the only variable under study is *how the load balancer spreads
connections across pods*.

- Any non-WebSocket GET is answered 200 so the ALB HTTP health check passes.
- Prints its own live connection count every 5s (`LIVE pod=<name> live=<n>`), so the
  per-pod distribution can be read from the pod logs as well as from the load client.
- ACTIVE_AUDIO=1: instead of holding the connection idle, stream frames both ways
  (send a frame every AUDIO_INTERVAL_MS, drain incoming frames) for the whole call,
  mimicking live bidirectional audio. Use to check whether the LB's in-flight
  accounting behaves differently for active vs. idle long-lived connections.

Pinned to the websockets library's legacy server API (see Dockerfile: websockets==12.0).
"""
import asyncio
import contextlib
import http
import os
import random

import websockets

PORT = int(os.getenv("PORT", "8080"))
POD = os.getenv("POD_NAME") or os.uname().nodename
AUDIO = os.getenv("ACTIVE_AUDIO") == "1"
AUDIO_MS = int(os.getenv("AUDIO_INTERVAL_MS", "150"))
FRAME = b"\x00" * int(os.getenv("AUDIO_FRAME_BYTES", "320"))  # ~a 20ms 8kHz PCM chunk

# SERVER_DECIDES_DURATION=1: the POD decides how long to hold each connection (by its role),
# ignoring the client's requested duration. LONG_HOLD=1 marks this pod as the "long" one.
# Used to create a stable per-pod skew (one pod holds long, others short) with all traffic
# routed normally through the LB -- no scale tricks.
SERVER_DECIDES = os.getenv("SERVER_DECIDES_DURATION") == "1"
LONG_HOLD = os.getenv("LONG_HOLD") == "1"
HOLD_LONG = (float(os.getenv("HOLD_LONG_MIN", "300")), float(os.getenv("HOLD_LONG_MAX", "600")))
HOLD_SHORT = (float(os.getenv("HOLD_SHORT_MIN", "5")), float(os.getenv("HOLD_SHORT_MAX", "20")))
_live = 0


def _server_dur():
    lo, hi = HOLD_LONG if LONG_HOLD else HOLD_SHORT
    return random.uniform(lo, hi)


async def _stream(ws, dur):
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


async def handler(ws, path):
    global _live
    _live += 1
    try:
        await ws.send(POD)  # first frame: tell the client which pod it landed on
        try:
            client_dur = float(await asyncio.wait_for(ws.recv(), timeout=30))
        except Exception:
            client_dur = 30.0
        dur = _server_dur() if SERVER_DECIDES else client_dur
        if AUDIO:
            await _stream(ws, dur)
        else:
            # hold the connection idle for ~dur seconds (or until the client closes first)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ws.wait_closed(), timeout=dur)
    finally:
        _live -= 1
        with contextlib.suppress(Exception):
            await ws.close(1001, "done")


async def process_request(path, request_headers):
    # Health check / any non-WebSocket GET -> 200. WS upgrades fall through (return None).
    try:
        upgrade = request_headers.get("Upgrade", "")
    except Exception:
        upgrade = ""
    if str(upgrade).lower() != "websocket":
        return http.HTTPStatus.OK, [("content-type", "text/plain")], b"ok\n"
    return None


async def _stats():
    while True:
        print(f"LIVE pod={POD} live={_live}", flush=True)
        await asyncio.sleep(5)


async def main():
    async with websockets.serve(
        handler, "0.0.0.0", PORT,
        process_request=process_request,
        ping_interval=20, ping_timeout=60, max_size=2 ** 16,
    ):
        print(f"dummy-ws up on :{PORT} pod={POD} active_audio={AUDIO}", flush=True)
        asyncio.create_task(_stats())
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
