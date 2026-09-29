"""The hard-drive-care read throttle."""
import threading
import time
import unittest

from dubchecker.models import Cancelled
from dubchecker.pipeline import ReadThrottle


class ReadThrottleTests(unittest.TestCase):
    def test_disabled_never_waits(self) -> None:
        throttle = ReadThrottle(False, 1, 30, threading.Event())
        started = time.monotonic()
        for _ in range(200):
            throttle.acquire()
            throttle.release()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(throttle.pauses, 0)

    def test_stop_is_noticed_even_when_disabled(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            ReadThrottle(False, 1, 30, cancel).acquire()

    def test_pauses_after_every_n_reads(self) -> None:
        waits: list[float] = []
        throttle = ReadThrottle(True, 2, 0.2, threading.Event(), waits.append)
        started = time.monotonic()
        for _ in range(5):
            throttle.acquire()
            throttle.release()
        self.assertEqual(throttle.pauses, 2)  # after reads 2 and 4, none after the last one
        self.assertGreaterEqual(time.monotonic() - started, 0.38)
        self.assertTrue(waits and waits[0] > 0)

    def test_parallel_batch_waits_for_all_reads_then_pauses(self) -> None:
        throttle = ReadThrottle(True, 3, 0.3, threading.Event())
        lock = threading.Lock()
        starts: list[float] = []
        ends: list[float] = []

        def reader() -> None:
            throttle.acquire()
            with lock:
                starts.append(time.monotonic())
            time.sleep(0.1)
            with lock:
                ends.append(time.monotonic())
            throttle.release()

        threads = [threading.Thread(target=reader) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        starts.sort()
        ends.sort()
        self.assertEqual(len(starts), 6)
        self.assertLess(starts[2] - starts[0], 0.09)                 # the first 3 run together
        self.assertGreaterEqual(starts[3], ends[2] + 0.28)           # the 4th waits for all 3, then the break
        self.assertEqual(throttle.pauses, 1)

    def test_stop_during_a_break(self) -> None:
        cancel = threading.Event()
        throttle = ReadThrottle(True, 1, 30, cancel)
        throttle.acquire()
        throttle.release()
        outcome: list[BaseException] = []

        def reader() -> None:
            try:
                throttle.acquire()
            except Cancelled as exc:
                outcome.append(exc)

        thread = threading.Thread(target=reader)
        started = time.monotonic()
        thread.start()
        time.sleep(0.2)
        cancel.set()
        thread.join(5)
        self.assertEqual(len(outcome), 1)
        self.assertLess(time.monotonic() - started, 2.5)


if __name__ == "__main__":
    unittest.main()
