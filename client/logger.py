"""
Thread-safe JSONL logger for all drone comms.
Every line is a JSON object with "t" (unix timestamp) and "type".
"""

import json
import threading
import time


class FlightLogger:
    def __init__(self, path: str):
        self._f = open(path, "a")
        self._lock = threading.Lock()
        self.path = path

    def log(self, event_type: str, **fields):
        record = {"t": time.time(), "type": event_type, **fields}
        line = json.dumps(record, separators=(",", ":"))
        with self._lock:
            self._f.write(line + "\n")
            self._f.flush()

    def close(self):
        with self._lock:
            self._f.close()
