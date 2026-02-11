# Dashboard: Live FPV + Telemetry

## Overview

Browser-based dashboard for live H.264 video and telemetry. Only external dependency: `websockets` library.

```
Relay (pi1.local:4900/4901)
    ↓ TCP telemetry       ↓ UDP video
Connection               VideoReceiver
    ↓ on_telemetry            ↓ on_packet
Dashboard ──────────────────────────────
    ↓ SSE (:8080)         ↓ WebSocket (:8081)
Browser (telemetry)   Browser (video)
    ↓ render()            ↓ WebCodecs
    DOM updates           Canvas
```

## Usage

```bash
uv run python main.py    # HTTP :8080, video WS :8081
```

Open in Chrome or Edge (WebCodecs required for video). Telemetry works in any browser.

## Video Pipeline

Low-latency H.264 decoding — target ~50ms (1 frame + network + decode).

### Python side (client/dashboard/__init__.py)

1. **GOL extraction** — `on_packet()` strips `\x00GOL` framing, extracts H.264 bytes
2. **NAL parser** — `_process_h264()` accumulates bytes, splits on Annex B start codes (`00 00 00 01`), classifies NAL type from first byte (`& 0x1F`)
3. **SPS/PPS caching** — NAL type 7 (SPS) and 8 (PPS) are cached; codec string extracted from SPS bytes 1-3
4. **avcC construction** — `_build_avcc()` creates the decoder configuration record from SPS+PPS
5. **Frame bundling** — IDR keyframes get SPS+PPS prepended; every frame is tagged with a type byte
6. **WebSocket server** — `websockets.sync.server` on port 8081, sends binary frames

### WebSocket binary message format

| Byte 0 | Payload | Description |
|--------|---------|-------------|
| `0x01` | codec string (UTF-8) + `\x00` + avcC bytes | Config (sent once per connect) |
| `0x02` | Annex B H.264 (SPS+PPS+IDR) | Keyframe |
| `0x03` | Annex B H.264 (slice NAL) | Delta frame |

### Browser side (client/dashboard/index.html)

1. **WebSocket** connects to `ws://host:8081`, binary mode
2. **Config (0x01)** — parse codec string + avcC, create and configure `VideoDecoder`
3. **Keyframe (0x02) / Delta (0x03)** — create `EncodedVideoChunk`, feed to decoder
4. **Render** — decoder output callback draws `VideoFrame` to `<canvas>` via `drawImage()`, then `frame.close()`
5. **Camera flip** — CSS `transform: scaleY(-1)` corrects the physically inverted camera

Auto-reconnects on WebSocket close (2s delay). Falls back to "WebCodecs not supported" message on Firefox/Safari.

### Backpressure

- Per-client `Queue(maxsize=60)` (~2.4s at 25fps)
- On full queue: flush all frames and re-push current frame
- Keyframes naturally repopulate after flush (SPS/PPS sent every ~3s by drone)
- WebSocket ping every 5s idle to detect dead connections

## Telemetry Pipeline

Server-Sent Events (SSE) on port 8080 — works in all browsers.

1. `on_telemetry()` callback converts `dataclass` to dict, pushes as `data:` SSE event
2. `on_relay_status()` pushes as named `relay` SSE event
3. Per-client `Queue(maxsize=50)`, oldest dropped on overflow
4. 15-second keepalive comments prevent SSE timeout
5. New clients get current relay state + latest telemetry immediately

## Endpoints

| Port | Route | Type | Content |
|------|-------|------|---------|
| 8080 | `GET /` | HTML | Dashboard page |
| 8080 | `GET /telemetry` | SSE | Telemetry + relay status events |
| 8080 | `GET /status` | JSON | Snapshot of telemetry + relay state |
| 8081 | WebSocket | Binary | H.264 video frames |

## Frontend Architecture

Declarative state pattern — single `state` object, one `render()` function derives all DOM. Event handlers only update state and call `render()`. No imperative DOM mutations.

### UI States

| Condition | Video panel | Telemetry | Banner |
|-----------|------------|-----------|--------|
| Default (no drone) | "No video feed" | Stale (dimmed) | Red: scanning |
| Connecting | "No video feed" | Stale | Yellow: connecting |
| Ready, no video yet | "Waiting for video..." | Live | Hidden |
| Ready + video | Canvas playing | Live | Hidden |
| Telemetry >3s old | -- | Stale + "Xs ago" | -- |

## Design Decisions

- **No ffmpeg** — previous pipeline (ffmpeg remux to fMP4 + MSE) added multi-second latency from probesize buffering, keyframe-boundary fragmentation, and MSE playout buffer. WebCodecs decodes frame-by-frame with no buffering.
- **No containers** — raw Annex B H.264 NALs. No fMP4, no MPEG-TS, no fragmentation overhead.
- **`websockets` library** — hand-rolled WebSocket over stdlib `BaseHTTPRequestHandler` was fragile (HTTP/1.0 vs 1.1 issues, buffering edge cases). Separate `websockets.sync.server` on its own port is clean and reliable.
- **Two ports** — HTTP server (:8080) handles HTML/SSE/status, WebSocket server (:8081) handles video. Clean separation, no protocol mixing in one handler. WS port injected into HTML via template substitution (`__WS_PORT__`).
- **Canvas not video element** — `<video>` requires MSE or a URL source. `<canvas>` + `drawImage()` renders `VideoFrame` objects directly from `VideoDecoder`.
- **CSS flip** — the drone camera is physically mounted upside-down. `scaleY(-1)` on the canvas corrects this without decoding overhead.
- **Dashboard serves immediately** — doesn't wait for drone connection. Relay status updates flow through SSE showing connection progression live.
