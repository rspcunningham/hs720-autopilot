"""
UDP video receiver — connects to relay's video port.
Sends a registration packet, then receives forwarded video.
"""

import socket
import threading
import time

from .logger import FlightLogger


class VideoReceiver:
    def __init__(self, host: str = "pi1.local", port: int = 4901,
                 logger: FlightLogger = None):
        self.host = host
        self.port = port
        self.logger = logger
        self._sock: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._packets = 0
        self._bytes = 0
        self._start_time = 0.0
        self._save_file = None
        self._first_bytes: bytes = b""
        self.on_packet = None  # callback(data: bytes)

    @property
    def stats(self) -> dict:
        with self._lock:
            elapsed = time.time() - self._start_time if self._start_time else 0
            return {
                "packets": self._packets,
                "bytes": self._bytes,
                "elapsed": round(elapsed, 1),
                "kbps": round(self._bytes * 8 / 1000 / elapsed, 1) if elapsed > 0 else 0,
                "first_bytes": self._first_bytes.hex() if self._first_bytes else "",
            }

    def start(self, save_path: str = None):
        """Register with relay for video and start receiving."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(2)

        # Send registration packet to relay — tells it our IP and port
        self._sock.sendto(b"video", (self.host, self.port))
        print(f"[*] Video: registered with relay {self.host}:{self.port}")

        if save_path:
            self._save_file = open(save_path, "wb")
            print(f"[*] Video: saving raw stream to {save_path}")

        self._start_time = time.time()
        self._running = True
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._save_file:
            self._save_file.close()
            self._save_file = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _recv_loop(self):
        # Re-register every 10 seconds to stay in relay's client list
        last_reg = time.time()
        while self._running and self._sock:
            try:
                data, addr = self._sock.recvfrom(65536)
                with self._lock:
                    self._packets += 1
                    self._bytes += len(data)
                    if not self._first_bytes and data:
                        self._first_bytes = data[:16]
                    pkt_num = self._packets

                if self._save_file:
                    self._save_file.write(data)

                if self.on_packet:
                    self.on_packet(data)

                if self.logger and pkt_num % 10 == 1:
                    self.logger.log("video_packet", seq=pkt_num, size=len(data),
                                    first_hex=data[:8].hex())

            except socket.timeout:
                pass
            except Exception as e:
                if self._running:
                    print(f"[-] Video recv error: {e}")
                break

            # Re-register periodically
            now = time.time()
            if now - last_reg > 10:
                try:
                    self._sock.sendto(b"video", (self.host, self.port))
                except Exception:
                    pass
                last_reg = now
