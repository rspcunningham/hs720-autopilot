"""Live drone dashboard — open http://localhost:8080 immediately, connect in background."""

import sys
import threading
from client import Connection, VideoReceiver, FlightLogger, Dashboard

logger = FlightLogger("logs/dashboard.jsonl")

host = sys.argv[1] if len(sys.argv) > 1 else "pi1.local"
conn = Connection(host, logger=logger)
video = VideoReceiver(host, logger=logger)

# Dashboard serves immediately — shows connection state as it progresses
dash = Dashboard(conn, video)
dash.serve(port=8080)

def connect_background():
    """Connect to gateway, start video. Drone connection is the gateway's job."""
    try:
        conn.connect()
        video.start()
    except Exception as e:
        print(f"[-] Gateway connection failed: {e}")

threading.Thread(target=connect_background, daemon=True).start()

try:
    input("Press Enter to stop...\n")
except KeyboardInterrupt:
    print()

dash.stop()
video.stop()
conn.disconnect()
