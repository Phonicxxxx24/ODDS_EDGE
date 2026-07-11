"""
OddsEdge Main Agent Runner
Starts the three core autonomous loops in background threads:
1. SSE Stream Consumer (detects STEAM signals on live txodds feed)
2. Result Fetcher (polls for finished games and scores signals/trades)
3. Pre-Kickoff Scheduler (scans upcoming games for TRI_SOURCE divergence)
"""

import threading
import time
from datetime import datetime, timezone

from agent.stream import StreamConsumer
from agent.results import run_result_loop
from agent.scheduler import scan_pre_kickoff_fixtures

def run_scheduler_loop():
    print("[Scheduler] Pre-kickoff scanner loop started (interval=5m)")
    while True:
        try:
            scan_pre_kickoff_fixtures()
        except Exception as e:
            print(f"[Scheduler] Unexpected error in scheduler loop: {e}")
        time.sleep(300)  # Check every 5 minutes

def main():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Starting OddsEdge autonomous agents...")

    # 1. Result checking loop
    res_thread = threading.Thread(target=run_result_loop, daemon=True)
    res_thread.start()

    # 2. Pre-kickoff scheduler loop (Tri-Source Divergence)
    sched_thread = threading.Thread(target=run_scheduler_loop, daemon=True)
    sched_thread.start()

    # 3. Stream Consumer (Steam Detection) - Runs in main thread to block
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Connecting to TxLINE SSE stream...")
    consumer = StreamConsumer()
    consumer.run_forever()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAgent gracefully terminated.")
