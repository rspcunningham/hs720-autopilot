"""
Live integration test via the Pi relay.
Usage: uv run python test_live.py [--host pi1.local] [--duration 30]
"""

import argparse
import os
import time
import sys

from hs710.connection import Connection
from hs710.logger import FlightLogger
from hs710.video import VideoReceiver


def main():
    parser = argparse.ArgumentParser(description="HS720 live telemetry + video test")
    parser.add_argument("--host", default="pi1.local", help="Relay host")
    parser.add_argument("--duration", type=int, default=30, help="Run duration in seconds")
    parser.add_argument("--save-video", action="store_true", help="Save raw video stream")
    args = parser.parse_args()

    os.makedirs("logs", exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    log_path = f"logs/{ts}.jsonl"
    video_path = f"logs/{ts}.video.raw" if args.save_video else None

    logger = FlightLogger(log_path)
    logger.log("session_start", host=args.host, duration=args.duration)
    print(f"[*] Logging to {log_path}")

    conn = Connection(args.host, logger=logger)
    video = VideoReceiver(args.host, logger=logger)

    telem_count = [0]

    def on_telemetry(state):
        telem_count[0] += 1
        print(f"  [TELEM #{telem_count[0]}] mode={state.mode_name} "
              f"alt={state.altitude}m dist={state.distance}m "
              f"sat={state.satellites} bat={state.voltage}V "
              f"spd={state.speed} "
              f"GPS=({state.lat:.7f}, {state.lon:.7f})")

    conn.on_telemetry = on_telemetry

    def on_raw_gol(cmd_id, rwbit, payload):
        preview = payload[:32].hex() if payload else ""
        print(f"  [GOL 0x{cmd_id:06x} rw={rwbit}] len={len(payload)} {preview}")

    conn.on_raw_gol = on_raw_gol

    # Connect to relay
    try:
        conn.connect()
    except Exception as e:
        print(f"[-] Connection failed: {e}")
        logger.log("error", msg=str(e))
        logger.close()
        sys.exit(1)

    # Wait for drone to be ready (relay may still be connecting)
    if conn.relay_state != "ready":
        print(f"[*] Waiting for drone (relay state: {conn.relay_state})...")
        if not conn.wait_ready(timeout=60):
            print("[-] Timeout waiting for drone")
            logger.log("error", msg="timeout waiting for ready")
            conn.disconnect()
            logger.close()
            sys.exit(1)

    # Start video
    try:
        video.start(save_path=video_path)
    except Exception as e:
        print(f"[-] Video start failed: {e}")
        logger.log("error", msg=f"video: {e}")

    # Run for duration
    print(f"\n[*] Running for {args.duration}s — Ctrl+C to stop early\n")
    start = time.time()
    last_video_print = start

    try:
        while time.time() - start < args.duration:
            time.sleep(1)
            now = time.time()
            if now - last_video_print >= 5:
                vs = video.stats
                if vs["packets"] > 0:
                    print(f"\n  [VIDEO] {vs['packets']} pkts, "
                          f"{vs['bytes']} bytes, {vs['kbps']} kbps "
                          f"first={vs['first_bytes'][:32]}")
                else:
                    print(f"\n  [VIDEO] No packets received ({vs['elapsed']:.0f}s)")
                logger.log("video_stats", **vs)
                last_video_print = now
    except KeyboardInterrupt:
        print("\n[*] Interrupted")

    # Summary
    elapsed = time.time() - start
    vs = video.stats
    summary = {
        "duration": round(elapsed, 1),
        "telemetry_packets": telem_count[0],
        "video_packets": vs["packets"],
        "video_bytes": vs["bytes"],
        "video_kbps": vs["kbps"],
    }
    if conn.state:
        s = conn.state
        summary["last_mode"] = s.mode_name
        summary["last_altitude"] = s.altitude
        summary["last_voltage"] = s.voltage
        summary["last_satellites"] = s.satellites
    logger.log("session_end", **summary)

    print(f"\n{'='*50}")
    print(f"  Duration: {elapsed:.1f}s")
    print(f"  Telemetry packets: {telem_count[0]}")
    print(f"  Video: {vs['packets']} pkts, {vs['bytes']} bytes, {vs['kbps']} kbps")
    if conn.state:
        s = conn.state
        print(f"  Last state: mode={s.mode_name} alt={s.altitude}m "
              f"bat={s.voltage}V sat={s.satellites}")
    print(f"  Log: {log_path}")
    print(f"{'='*50}")

    video.stop()
    conn.disconnect()
    logger.close()


if __name__ == "__main__":
    main()
