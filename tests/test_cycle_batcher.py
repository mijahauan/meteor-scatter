"""Tests for meteor_scatter.core.cycle_batcher.

Covers per-(cycle, rx_source) batching of decoded MSK144 spots before
they hit the SQLite sink.  Exercises:

  * Cycle-boundary math for the MSK144 15 s T/R period
  * Batch keying — same cycle / different rx → distinct batches
  * Deadline behaviour
  * Cycle-commit log line in WSPR-parity format (parseable by
    ``smd watch``)
  * Shutdown drains pending batches
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from meteor_scatter.core.cycle_batcher import (
    MeteorScatterCycleBatcher,
    _cycle_start,
    _cycle_iso,
    _freq_to_band_name,
)


class FakeWriter:
    """Stand-in for sigmond.hamsci_sink.Writer.

    Tracks each insert() / flush() / close() call so tests can
    inspect what the batcher's writer thread wrote.
    """

    def __init__(self, batch_rows: int = 200):
        self.batch_rows = batch_rows
        self.inserts: list[list[dict]] = []
        self.flushes = 0
        self.closed = False
        self.is_noop = False
        self.health = "ok"
        self._lock = threading.Lock()

    def insert(self, rows):
        with self._lock:
            self.inserts.append(list(rows))

    def flush(self):
        with self._lock:
            self.flushes += 1

    def close(self):
        self.closed = True


def _row(*, mode="msk144", utc=(2026, 5, 20, 19, 14, 0), freq=28130000, **kw):
    """Build a minimal msk144.spots row dict for the batcher."""
    return {
        "time": datetime(*utc, tzinfo=timezone.utc),
        "mode": mode,
        "frequency": freq,
        "tx_call": kw.get("tx_call", "K1ABC"),
        "grid": kw.get("grid", "FN42"),
        "message": "K1ABC W1XYZ FN42",
    }


class CycleBoundaryTests(unittest.TestCase):

    def test_msk144_cycle_floors_to_15s(self):
        ts = datetime(2026, 5, 20, 19, 14, 22, tzinfo=timezone.utc)
        start = _cycle_start(ts, "msk144")
        self.assertEqual(start.second, 15)
        self.assertEqual(start.microsecond, 0)

    def test_iso_renders_integer_second(self):
        # MSK144's 15 s cycles always land on an integer second → ".0Z".
        ts = datetime(2026, 5, 20, 19, 14, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(_cycle_iso(ts), "2026-05-20T19:14:15.0Z")

    def test_unknown_mode_still_uses_15s_grid(self):
        """A bogus mode tag must not crash the floor — this client only
        ever emits msk144, and an unexpected tag falls back to the same
        15 s grid rather than crashing."""
        ts = datetime(2026, 5, 20, 19, 14, 22, tzinfo=timezone.utc)
        start = _cycle_start(ts, "weird-mode")
        self.assertEqual(start.second, 15)


class FreqToBandTests(unittest.TestCase):

    def test_standard_msk144_freqs_map_to_band(self):
        self.assertEqual(_freq_to_band_name(28130000), "10")   # 10 m dial
        self.assertEqual(_freq_to_band_name(50260000), "6")    # 6 m dial

    def test_non_standard_freq_falls_back_to_khz_bucket(self):
        # 28200000 isn't a known MSK144 dial; nearest 100 kHz bucket.
        tag = _freq_to_band_name(28200000)
        self.assertTrue(tag.endswith("k"), f"unexpected tag: {tag}")


class BatcherFlushTests(unittest.TestCase):

    def _make(self, *, cycle_deadline=0.05):
        """Spin up a batcher with a tight deadline so tests fire quickly."""
        writer = FakeWriter()
        batcher = MeteorScatterCycleBatcher(
            writer_factory=lambda batch_rows: writer,
            cycle_deadline_sec=cycle_deadline,
        )
        batcher.start()
        return batcher, writer

    def test_batch_flushes_after_deadline(self):
        batcher, writer = self._make()
        try:
            batcher.add(
                [_row()],
                rx_source="radiod:bee1-status.local",
                radiod_id="bee1",
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not writer.inserts:
                time.sleep(0.02)
        finally:
            batcher.stop()
        self.assertEqual(len(writer.inserts), 1)
        self.assertEqual(len(writer.inserts[0]), 1)
        self.assertEqual(writer.inserts[0][0]["mode"], "msk144")

    def test_same_cycle_different_rx_yields_separate_batches(self):
        """Multi-source decode of the same cycle: each rx flushes its
        own batch so per-rx visibility and cross-rx dedup both work."""
        batcher, writer = self._make()
        try:
            batcher.add(
                [_row(tx_call="X1")],
                rx_source="radiod:bee1-status.local", radiod_id="bee1",
            )
            batcher.add(
                [_row(tx_call="X2")],
                rx_source="radiod:bee2-status.local", radiod_id="bee2",
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and len(writer.inserts) < 2:
                time.sleep(0.02)
        finally:
            batcher.stop()
        self.assertEqual(len(writer.inserts), 2)
        all_calls = {row["tx_call"]
                     for batch in writer.inserts for row in batch}
        self.assertEqual(all_calls, {"X1", "X2"})

    def test_same_rx_same_cycle_coalesces(self):
        """Two adds in one cycle from one rx → one batch with both
        rows.  Confirms the dict-keying behaviour."""
        batcher, writer = self._make()
        try:
            batcher.add(
                [_row(tx_call="A")],
                rx_source="radiod:rx-status.local", radiod_id="rx",
            )
            batcher.add(
                [_row(tx_call="B")],
                rx_source="radiod:rx-status.local", radiod_id="rx",
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not writer.inserts:
                time.sleep(0.02)
        finally:
            batcher.stop()
        self.assertEqual(len(writer.inserts), 1)
        calls = {row["tx_call"] for row in writer.inserts[0]}
        self.assertEqual(calls, {"A", "B"})

    def test_log_line_format_matches_wspr_parity(self):
        """The cycle commit log line must include rx, mode, spot count,
        and bands=[...] in the order the watch parser expects."""
        import re
        batcher, writer = self._make()
        with self.assertLogs(
            "meteor_scatter.core.cycle_batcher", level=logging.INFO,
        ) as cm:
            try:
                batcher.add(
                    [
                        _row(freq=28130000),
                        _row(freq=28130000),
                        _row(freq=50260000),
                    ],
                    rx_source="radiod:bee1-status.local",
                    radiod_id="bee1",
                )
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline and not writer.inserts:
                    time.sleep(0.02)
            finally:
                batcher.stop()
        joined = "\n".join(cm.output)
        pat = re.compile(
            r"cycle UTC \S+ rx=radiod:bee1-status\.local mode=msk144 "
            r"→ 3 spots in psk\.spots "
            r"\(sqlite write \d+ ms\) bands=\[",
        )
        self.assertRegex(joined, pat)
        # Both bands should appear in the breakdown (10 m and 6 m).
        self.assertRegex(joined, r"bands=\[[^]]*10:2")
        self.assertRegex(joined, r"bands=\[[^]]*6:1")

    def test_stop_drains_pending_batches(self):
        """A batch under its deadline at stop() must still flush —
        we don't want shutdown to silently drop just-received spots."""
        batcher, writer = self._make(cycle_deadline=10.0)  # long deadline
        batcher.add(
            [_row()],
            rx_source="radiod:bee1-status.local", radiod_id="bee1",
        )
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and batcher.writer is None:
            time.sleep(0.02)
        batcher.stop()
        self.assertEqual(len(writer.inserts), 1)
        self.assertEqual(len(writer.inserts[0]), 1)


class SuperviseTests(unittest.TestCase):
    """_supervise turns a silent background-thread death into a loud log +
    backed-off auto-restart, so a crashing batcher/lifetime/stats loop does
    not silently stop its subsystem."""

    def test_restarts_loop_until_clean_return(self):
        from meteor_scatter.core import cycle_batcher as cb
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom")
            return  # clean stop on the 3rd attempt

        with mock.patch.object(cb.time, "sleep", lambda *_a: None):
            cb._supervise("t", lambda: True, fn)
        self.assertEqual(calls["n"], 3)

    def test_no_restart_once_not_alive(self):
        from meteor_scatter.core import cycle_batcher as cb
        calls = {"n": 0}
        alive = {"v": True}

        def fn():
            calls["n"] += 1
            alive["v"] = False  # daemon shutting down
            raise RuntimeError("crash during shutdown")

        with mock.patch.object(cb.time, "sleep", lambda *_a: None):
            cb._supervise("t", lambda: alive["v"], fn)
        self.assertEqual(calls["n"], 1)

    def test_clean_loop_runs_once(self):
        from meteor_scatter.core import cycle_batcher as cb
        calls = {"n": 0}
        cb._supervise("t", lambda: True, lambda: calls.__setitem__("n", calls["n"] + 1))
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
