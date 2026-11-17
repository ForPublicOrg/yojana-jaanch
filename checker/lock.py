"""One checker at a time per project folder.

Several queues (one per scheme) each run the checker after an area. The checker takes
data/state/checker.lock for the whole run, so the cases.json and areas.json it writes always come
from one run. A second checker waits for the first to finish. The operating system drops the lock
when the process ends, even after a crash."""
import os
import time


class Busy(Exception):
    pass


class CheckerLock:
    def __init__(self, path, wait_seconds=1800, poll=1.0, sleep=time.sleep, clock=time.monotonic):
        self.path = str(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.f = open(self.path, "a+b")
        deadline = clock() + wait_seconds
        while True:
            try:
                self._lock()
                return
            except OSError:
                if clock() >= deadline:
                    self.f.close()
                    raise Busy(f"another checker has held {self.path} for over {wait_seconds:.0f} seconds") from None
                sleep(poll)

    def _lock(self):
        if os.name == "nt":
            import msvcrt
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self):
        if self.f.closed:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
