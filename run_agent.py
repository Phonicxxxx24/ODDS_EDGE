import os
import sys
import time
import threading
from datetime import datetime, timezone

# ── path setup so we can import agent.* from the project root ──────────────
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.stream import StreamConsumer
from agent.results import run_result_loop
from agent.scheduler import scan_pre_kickoff_fixtures

class Tee:
    def __init__(self, original, file_obj):
        self.original = original
        self.file_obj = file_obj

    def write(self, data):
        try:
            self.original.write(data)
        except UnicodeEncodeError:
            # Fallback by replacing unencodable characters
            enc = getattr(self.original, 'encoding', 'ascii') or 'ascii'
            self.original.write(data.encode(enc, errors='replace').decode(enc))
        self.original.flush()

        try:
            self.file_obj.write(data)
        except UnicodeEncodeError:
            self.file_obj.write(data.encode('utf-8', errors='replace').decode('utf-8'))
        self.file_obj.flush()

    def flush(self):
        self.original.flush()
        self.file_obj.flush()

def run_scheduler_loop():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] [Scheduler] Pre-kickoff scanner loop started (interval=5m)")
    while True:
        try:
            scan_pre_kickoff_fixtures()
        except Exception as e:
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] [Scheduler] Unexpected error: {e}")
        time.sleep(300)

def main():
    # Reconfigure output encoding to UTF-8 if supported
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
    if hasattr(sys.stderr, 'reconfigure'):
        try:
            sys.stderr.reconfigure(encoding='utf-8')
        except Exception:
            pass

    # Make sure database is initialized
    from agent.database import init_db
    init_db()

    # Configure stdout/stderr teeing to agent.log
    log_file = open("agent.log", "a", encoding="utf-8")
    sys.stdout = Tee(sys.stdout, log_file)
    sys.stderr = Tee(sys.stderr, log_file)

    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] === OddsEdge Agent Started ===")

    # 1. Result-fetching loop thread
    res_thread = threading.Thread(target=run_result_loop, daemon=True)
    res_thread.start()
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Result fetcher loop thread started")

    # 2. Pre-kickoff scheduler loop thread
    sched_thread = threading.Thread(target=run_scheduler_loop, daemon=True)
    sched_thread.start()
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Pre-kickoff scheduler thread started")

    # 3. SSE stream consumer (runs in main thread)
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] SSE Stream consumer starting...")
    consumer = StreamConsumer()
    consumer.run_forever()

if __name__ == "__main__":
    main()
