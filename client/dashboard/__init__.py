"""
Live drone dashboard — serves telemetry SSE + H.264 video in browser.
Zero external deps beyond websockets library.

Video pipeline: GOL → NAL parser → WebSocket → WebCodecs VideoDecoder → Canvas
Target ~50ms latency. No ffmpeg, no containers, no MSE.
"""

import json
import pathlib
import queue
import struct
import sys
import threading
import time
from dataclasses import asdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

from websockets.sync.server import serve as ws_serve

_HTML_TEMPLATE = (pathlib.Path(__file__).parent / "index.html").read_text()


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            pass  # Browser disconnected — normal
        else:
            super().handle_error(request, client_address)


class Dashboard:
    def __init__(self, conn, video):
        self._conn = conn
        self._video = video
        self._http_server = None
        self._ws_server = None
        self._http_thread = None
        self._ws_thread = None
        self._ws_port = 8081

        # Video pipeline — NAL parser
        self._nal_buf = bytearray()
        self._sps = b""
        self._pps = b""
        self._codec = ""
        self._config_msg = b""  # cached config message for new WS clients
        self._next_slice_is_key = False  # SPS+PPS seen, next slice is a keyframe
        self._keyframe_sent = False  # don't send deltas until first keyframe
        self._last_keyframe = b""  # cached for late-joining clients
        self._ws_clients: list = []  # active WebSocket connections (direct send)
        self._ws_lock = threading.Lock()

        # Telemetry SSE
        self._sse_clients: list[queue.Queue] = []
        self._sse_lock = threading.Lock()

        # Latest state
        self._latest_telemetry = None
        self._latest_relay = None
        self._latest_control = None
        self._latest_ptz = None
        self._latest_device = None

        # Chain callbacks
        self._orig_on_telemetry = conn.on_telemetry
        self._orig_on_relay_status = conn.on_relay_status
        self._orig_on_control_info = conn.on_control_info
        self._orig_on_ptz_info = conn.on_ptz_info
        self._orig_on_device_info = conn.on_device_info
        self._orig_on_packet = video.on_packet
        conn.on_telemetry = self._on_telemetry
        conn.on_relay_status = self._on_relay_status
        conn.on_control_info = self._on_control_info
        conn.on_ptz_info = self._on_ptz_info
        conn.on_device_info = self._on_device_info
        video.on_packet = self._on_packet

    def serve(self, port: int = 8080, ws_port: int = 8081):
        self._ws_port = ws_port

        # HTTP server (HTML, SSE telemetry, status)
        handler = _make_handler(self)
        self._http_server = _ThreadingHTTPServer(("0.0.0.0", port), handler)
        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever, daemon=True)
        self._http_thread.start()

        # WebSocket server (video)
        self._ws_server = ws_serve(
            self._ws_handler, "0.0.0.0", ws_port)
        self._ws_thread = threading.Thread(
            target=self._ws_server.serve_forever, daemon=True)
        self._ws_thread.start()

        print(f"[*] Dashboard: http://localhost:{port} (video ws://localhost:{ws_port})")

    def stop(self):
        if self._http_server:
            self._http_server.shutdown()
            self._http_server = None
        if self._ws_server:
            self._ws_server.shutdown()
            self._ws_server = None
        self._conn.on_telemetry = self._orig_on_telemetry
        self._conn.on_relay_status = self._orig_on_relay_status
        self._conn.on_control_info = self._orig_on_control_info
        self._conn.on_ptz_info = self._orig_on_ptz_info
        self._conn.on_device_info = self._orig_on_device_info
        self._video.on_packet = self._orig_on_packet
        print("[*] Dashboard stopped")

    # --- WebSocket video handler ---

    def _ws_handler(self, ws):
        # Send cached config + keyframe for immediate playback
        if self._config_msg:
            ws.send(self._config_msg)
            if self._last_keyframe:
                ws.send(self._last_keyframe)
                print(f"[ws] Client connected, sent config + keyframe ({len(self._last_keyframe)}B)")
            else:
                print(f"[ws] Client connected, sent config, waiting for keyframe")
        else:
            print(f"[ws] Client connected, no config yet")

        # Register for direct video push (no queue, no thread hop)
        with self._ws_lock:
            self._ws_clients.append(ws)
        try:
            for _ in ws:  # blocks until close; video pushed via _push_video
                pass
        except Exception:
            pass
        finally:
            with self._ws_lock:
                try:
                    self._ws_clients.remove(ws)
                except ValueError:
                    pass
            print("[ws] Client disconnected")

    # --- Callbacks ---

    def _on_telemetry(self, state):
        d = asdict(state)
        d["mode_name"] = state.mode_name
        d["flying"] = state.flying
        d["signal_strength"] = state.signal_strength
        d["app_control"] = state.app_control
        d["low_battery"] = state.low_battery
        d["critical_battery"] = state.critical_battery
        d["initialized"] = state.initialized
        d["gyro_error"] = state.gyro_error
        d["baro_error"] = state.baro_error
        d["compass_error"] = state.compass_error
        d["gps_error"] = state.gps_error
        d["headless"] = state.headless
        d["recording"] = state.recording
        self._latest_telemetry = d
        self._push_sse(f"data: {json.dumps(d)}\n\n")
        if self._orig_on_telemetry:
            self._orig_on_telemetry(state)

    def _on_relay_status(self, status):
        self._latest_relay = status
        self._push_sse(f"event: relay\ndata: {json.dumps(status)}\n\n")
        if self._orig_on_relay_status:
            self._orig_on_relay_status(status)

    def _on_control_info(self, ctrl):
        d = asdict(ctrl)
        self._latest_control = d
        self._push_sse(f"event: control\ndata: {json.dumps(d)}\n\n")
        if self._orig_on_control_info:
            self._orig_on_control_info(ctrl)

    def _on_ptz_info(self, ptz):
        d = asdict(ptz)
        d["status_name"] = ptz.status_name
        self._latest_ptz = d
        self._push_sse(f"event: ptz\ndata: {json.dumps(d)}\n\n")
        if self._orig_on_ptz_info:
            self._orig_on_ptz_info(ptz)

    def _on_device_info(self, dev):
        d = asdict(dev)
        self._latest_device = d
        self._push_sse(f"event: device\ndata: {json.dumps(d)}\n\n")
        if self._orig_on_device_info:
            self._orig_on_device_info(dev)

    def _on_packet(self, data: bytes):
        # GOL video packet: \x00GOL(16) + sub-header(57) + h264_len(4) + h264 + \xFFGOL(4)
        if len(data) < 81 or data[0:4] != b"\x00GOL" or data[-4:] != b"\xffGOL":
            return
        h264_len = struct.unpack_from("<I", data, 73)[0]
        h264 = data[77:77 + h264_len]
        if h264:
            self._process_h264(h264)
        if self._orig_on_packet:
            self._orig_on_packet(data)

    # --- SSE ---

    def _push_sse(self, event_data):
        with self._sse_lock:
            dead = []
            for i, q in enumerate(self._sse_clients):
                try:
                    q.put_nowait(event_data)
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(event_data)
                    except queue.Full:
                        dead.append(i)
            for i in reversed(dead):
                self._sse_clients.pop(i)

    def add_sse_client(self):
        q = queue.Queue(maxsize=50)
        with self._sse_lock:
            self._sse_clients.append(q)
        return q

    def remove_sse_client(self, q):
        with self._sse_lock:
            try:
                self._sse_clients.remove(q)
            except ValueError:
                pass

    # --- Video pipeline (NAL parser + direct WS send) ---

    def _push_video(self, msg: bytes):
        """Send a framed video message directly to all WebSocket clients."""
        with self._ws_lock:
            dead = []
            for i, ws in enumerate(self._ws_clients):
                try:
                    ws.send(msg)
                except Exception:
                    dead.append(i)
            for i in reversed(dead):
                self._ws_clients.pop(i)

    _START_CODE = b"\x00\x00\x00\x01"

    def _handle_nal(self, nal: bytes) -> bytes | None:
        """Process a NAL unit. Returns a framed message to send, or None."""
        if not nal:
            return None
        nal_type = nal[0] & 0x1F
        if nal_type == 7:  # SPS
            self._sps = nal
            self._codec = f"avc1.{nal[1]:02X}{nal[2]:02X}{nal[3]:02X}"
        elif nal_type == 8:  # PPS
            self._pps = nal
            self._next_slice_is_key = True
            if self._sps:
                self._config_msg = self._build_config_msg()
                print(f"[*] Video: codec={self._codec}, SPS={len(self._sps)}B, PPS={len(self._pps)}B")
        elif nal_type == 5 or (nal_type == 1 and self._next_slice_is_key):
            if self._sps and self._pps:
                frame = (self._START_CODE + self._sps
                         + self._START_CODE + self._pps
                         + self._START_CODE + nal)
                msg = b"\x02" + frame
                self._last_keyframe = msg
                self._keyframe_sent = True
                self._next_slice_is_key = False
                return msg
        elif nal_type == 1:
            if self._keyframe_sent:
                return b"\x03" + self._START_CODE + nal
        return None

    def _process_h264(self, data: bytes):
        """Accumulate H.264 bytes, split on Annex B start codes, process NALs."""
        self._nal_buf.extend(data)
        buf = self._nal_buf

        positions = []
        search_start = 0
        while True:
            idx = buf.find(self._START_CODE, search_start)
            if idx < 0:
                break
            positions.append(idx)
            search_start = idx + 4

        if len(positions) < 2:
            return  # Need at least 2 start codes to extract a complete NAL

        for j in range(len(positions) - 1):
            nal = bytes(buf[positions[j] + 4:positions[j + 1]])
            msg = self._handle_nal(nal)
            if msg:
                self._push_video(msg)

        # Keep the last start code onward (incomplete NAL)
        self._nal_buf = bytearray(buf[positions[-1]:])

    def _build_avcc(self) -> bytes:
        """Build avcC decoder configuration record from cached SPS+PPS."""
        sps = self._sps
        pps = self._pps
        return bytes([
            1,             # configurationVersion
            sps[1],        # AVCProfileIndication
            sps[2],        # profile_compatibility
            sps[3],        # AVCLevelIndication
            0xFF,          # lengthSizeMinusOne = 3 (4-byte lengths)
            0xE1,          # numOfSequenceParameterSets = 1
        ]) + struct.pack(">H", len(sps)) + sps + bytes([
            1,             # numOfPictureParameterSets = 1
        ]) + struct.pack(">H", len(pps)) + pps

    def _build_config_msg(self) -> bytes:
        """Build config message: 0x01 + codec UTF-8 + 0x00 + avcC bytes."""
        return b"\x01" + self._codec.encode() + b"\x00" + self._build_avcc()


def _make_handler(dash: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, /, *args: object) -> None:  # type: ignore[override]
            pass

        def do_GET(self):
            if self.path == "/":
                self._serve_html()
            elif self.path == "/telemetry":
                self._serve_sse()
            elif self.path == "/status":
                self._serve_status()
            else:
                self.send_error(404)

        def _serve_html(self):
            html = _HTML_TEMPLATE.replace("__WS_PORT__", str(dash._ws_port))
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            q = dash.add_sse_client()
            try:
                relay_state = dash._conn.relay_state
                if relay_state and relay_state != "unknown":
                    self.wfile.write(
                        f"event: relay\ndata: {json.dumps({'state': relay_state})}\n\n".encode())
                if dash._latest_telemetry:
                    self.wfile.write(
                        f"data: {json.dumps(dash._latest_telemetry)}\n\n".encode())
                if dash._latest_device:
                    self.wfile.write(
                        f"event: device\ndata: {json.dumps(dash._latest_device)}\n\n".encode())
                if dash._latest_control:
                    self.wfile.write(
                        f"event: control\ndata: {json.dumps(dash._latest_control)}\n\n".encode())
                if dash._latest_ptz:
                    self.wfile.write(
                        f"event: ptz\ndata: {json.dumps(dash._latest_ptz)}\n\n".encode())
                self.wfile.flush()
                last_ka = time.time()
                while True:
                    try:
                        event = q.get(timeout=1)
                        self.wfile.write(event.encode())
                        self.wfile.flush()
                    except queue.Empty:
                        if time.time() - last_ka > 15:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            last_ka = time.time()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                dash.remove_sse_client(q)

        def _serve_status(self):
            body = json.dumps({
                "telemetry": dash._latest_telemetry,
                "relay": dash._latest_relay,
                "control": dash._latest_control,
                "ptz": dash._latest_ptz,
                "device": dash._latest_device,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler
