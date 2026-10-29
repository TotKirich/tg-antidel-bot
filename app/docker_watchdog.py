"""Supervise an event-loop pulse; Docker restarts this process on failure."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def run():
    os.umask(0o077)
    root = Path(os.environ.get('TG_GUARD_DATA_DIR', '/data'))
    child = subprocess.Popen([sys.executable, '/app/userbot.py'])
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
        child.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    started = time.time()
    try:
        while child.poll() is None:
            try:
                pulse = float((root / '.heartbeat').read_text())
            except (OSError, ValueError):
                pulse = 0
            if stopping or (time.time() - max(started, pulse) > (180 if pulse < started else 150)):
                child.terminate()
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                return 0 if stopping else 1
            time.sleep(2)
        return child.returncode
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=30)


if __name__ == '__main__':
    sys.exit(run())
