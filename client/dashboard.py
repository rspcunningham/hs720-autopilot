"""
Live drone dashboard — serves telemetry SSE + H.264/fMP4 video in browser.
Zero external dependencies: stdlib + ffmpeg only.

Video pipeline: strip GOL header → ffmpeg remux (no transcode) → fMP4 → MSE
Browser gets native H.264 decoding (GPU-accelerated), zero quality loss.
"""

import json
import pathlib
import queue
import struct
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

_HTML_PAGE = (pathlib.Path(__file__).parent / "dashboard.html").read_text()


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
        self._server = None
        self._server_thread = None

        # Video pipeline
        self._ffmpeg = None
        self._ffmpeg_thread = None
        self._init_segment = b""
        self._init_ready = threading.Event()
        self._codec = "avc1.42E01E"
        self._video_clients: list[queue.Queue] = []
        self._video_lock = threading.Lock()

        # Telemetry SSE
        self._sse_clients: list[queue.Queue] = []
        self._sse_lock = threading.Lock()

        # Latest state
        self._latest_telemetry = None
        self._latest_relay = None

        # Chain callbacks
        self._orig_on_telemetry = conn.on_telemetry
        self._orig_on_relay_status = conn.on_relay_status
        self._orig_on_packet = video.on_packet
        conn.on_telemetry = self._on_telemetry
        conn.on_relay_status = self._on_relay_status
        video.on_packet = self._on_packet

    def serve(self, port: int = 8080):
        self._start_ffmpeg()
        handler = _make_handler(self)
        self._server = _ThreadingHTTPServer(("0.0.0.0", port), handler)
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._server_thread.start()
        print(f"[*] Dashboard: http://localhost:{port}")

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server = None
        self._stop_ffmpeg()
        self._conn.on_telemetry = self._orig_on_telemetry
        self._conn.on_relay_status = self._orig_on_relay_status
        self._video.on_packet = self._orig_on_packet
        print("[*] Dashboard stopped")

    # --- Callbacks ---

    def _on_telemetry(self, state):
        d = asdict(state)
        d["mode_name"] = state.mode_name
        d["flying"] = state.flying
        self._latest_telemetry = d
        self._push_sse(f"data: {json.dumps(d)}\n\n")
        if self._orig_on_telemetry:
            self._orig_on_telemetry(state)

    def _on_relay_status(self, status):
        self._latest_relay = status
        self._push_sse(f"event: relay\ndata: {json.dumps(status)}\n\n")
        if self._orig_on_relay_status:
            self._orig_on_relay_status(status)

    def _on_packet(self, data: bytes):
        # GOL video packet: \x00GOL(4) + pad(4) + len_u32le(4) + pad(4) + payload + \xFFGOL(4)
        if len(data) < 20 or data[0:4] != b"\x00GOL":
            return
        payload_len = struct.unpack_from("<I", data, 8)[0]
        h264 = data[16:16 + payload_len]
        if self._ffmpeg and self._ffmpeg.stdin and h264:
            try:
                self._ffmpeg.stdin.write(h264)
                self._ffmpeg.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
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

    # --- Video pipeline ---

    def add_video_client(self):
        q = queue.Queue(maxsize=300)
        self._init_ready.wait(timeout=10)
        with self._video_lock:
            self._video_clients.append(q)
        return q

    def remove_video_client(self, q):
        with self._video_lock:
            try:
                self._video_clients.remove(q)
            except ValueError:
                pass

    def _push_video(self, chunk):
        with self._video_lock:
            for q in self._video_clients:
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    pass  # Skip for slow clients

    def _start_ffmpeg(self):
        self._ffmpeg = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "warning",
                "-probesize", "32768",
                "-analyzeduration", "0",
                "-fflags", "nobuffer+genpts",
                "-flags", "low_delay",
                "-f", "h264", "-i", "pipe:0",
                "-c:v", "copy",
                "-f", "mp4",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "-flush_packets", "1",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._ffmpeg_thread = threading.Thread(
            target=self._ffmpeg_reader, daemon=True)
        self._ffmpeg_thread.start()
        # Drain stderr to prevent pipe deadlock
        threading.Thread(target=self._ffmpeg_stderr, daemon=True).start()

    def _stop_ffmpeg(self):
        if self._ffmpeg:
            try:
                if self._ffmpeg.stdin:
                    self._ffmpeg.stdin.close()
            except Exception:
                pass
            try:
                self._ffmpeg.terminate()
                self._ffmpeg.wait(timeout=3)
            except Exception:
                try:
                    self._ffmpeg.kill()
                except Exception:
                    pass
            self._ffmpeg = None

    def _ffmpeg_stderr(self):
        """Drain ffmpeg stderr to prevent pipe deadlock."""
        assert self._ffmpeg and self._ffmpeg.stderr
        for line in self._ffmpeg.stderr:
            text = line.decode(errors="replace").strip()
            if text:
                print(f"[ffmpeg] {text}")

    def _ffmpeg_reader(self):
        """Read fMP4 from ffmpeg stdout. Capture init segment, then fan out."""
        assert self._ffmpeg and self._ffmpeg.stdout
        stdout = self._ffmpeg.stdout
        buf = b""
        init_captured = False
        while True:
            chunk = stdout.read(4096)
            if not chunk:
                break
            if not init_captured:
                buf += chunk
                # Init segment = ftyp + moov. Ends where first moof starts.
                idx = buf.find(b"moof")
                if idx >= 4:
                    self._init_segment = buf[:idx - 4]
                    self._codec = self._extract_codec()
                    self._init_ready.set()
                    print(f"[*] Video: init segment captured ({len(self._init_segment)} bytes, codec={self._codec})")
                    self._push_video(buf[idx - 4:])
                    init_captured = True
                continue
            self._push_video(chunk)

    def _extract_codec(self):
        """Parse avcC box from init segment to get exact codec string."""
        idx = self._init_segment.find(b"avcC")
        if idx >= 0 and idx + 7 < len(self._init_segment):
            profile = self._init_segment[idx + 5]
            compat = self._init_segment[idx + 6]
            level = self._init_segment[idx + 7]
            return f"avc1.{profile:02X}{compat:02X}{level:02X}"
        return "avc1.42E01E"


def _make_handler(dash: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, /, *args: object) -> None:  # type: ignore[override]
            pass

        def do_GET(self):
            if self.path == "/":
                self._serve_html()
            elif self.path == "/video":
                self._serve_video()
            elif self.path == "/telemetry":
                self._serve_sse()
            elif self.path == "/status":
                self._serve_status()
            else:
                self.send_error(404)

        def _serve_html(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_HTML_PAGE.encode())

        def _serve_video(self):
            q = dash.add_video_client()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()
            try:
                if dash._init_segment:
                    self.wfile.write(dash._init_segment)
                    self.wfile.flush()
                while True:
                    try:
                        chunk = q.get(timeout=5)
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except queue.Empty:
                        continue
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                dash.remove_video_client(q)

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
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "telemetry": dash._latest_telemetry,
                "relay": dash._latest_relay,
                "codec": dash._codec,
            }).encode())

    return Handler
