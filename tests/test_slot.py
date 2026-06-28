"""Tests for SlotWorker: clock-driven slot harvesting + jt9 decoder lifecycle.

Slot-boundary math now lives in ka9q.SlotClock (see ka9q-python
tests/test_slot_clock.py); these tests cover the SlotWorker's use of it —
harvesting completed slots, extracting their sample window from the ring by
absolute offset, writing the WAV, and the hung-decoder reap path.
"""

import io
import threading
import tempfile
import unittest
from pathlib import Path

import numpy as np

from ka9q import SlotClock

from meteor_scatter.core.ring import Ring
from meteor_scatter.core.slot import SlotWorker

SR = 12000


def _make_worker(tmpdir, *, mode="msk144", cadence=15.0, ring=None,
                 keep_wav=False, decoder_path=""):
    """Construct a SlotWorker wired to a SlotClock + ring + latest-rtp box."""
    if ring is None:
        ring = Ring(max_seconds=60, sample_rate=SR)
    clock = SlotClock(cadence_sec=cadence, sample_rate=SR, settle_sec=1.5)
    lock = threading.Lock()
    box = {"rtp": None}
    # get_anchor_utc_now returns the current UTC of the fixed anchor_rtp.  In
    # these tests there is no radiod slide, so it is just the anchored utc
    # (None until anchored).  ``box['anchor_utc']`` lets a test simulate a slide.
    def _anchor_utc_now():
        if "anchor_utc" in box:
            return box["anchor_utc"]
        return clock._anchor_utc  # None until clock.anchor() is called
    worker = SlotWorker(
        ring=ring,
        mode=mode,
        frequency_hz=28130000,
        cadence_sec=cadence,
        spool_dir=Path(tmpdir) / mode,
        log_fd=io.StringIO(),
        decoder_path=decoder_path,
        clock=clock,
        get_latest_rtp=lambda: box["rtp"],
        clock_lock=lock,
        get_anchor_utc_now=_anchor_utc_now,
        keep_wav=keep_wav,
    )
    return worker, clock, ring, box


class SlotHarvestTickTests(unittest.TestCase):
    """tick() harvests a completed slot and writes its WAV."""

    def test_tick_writes_wav_when_slot_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(tmpdir, keep_wav=True)
            # Anchor on a grid point (900000000 % 15 == 0) -> boundary0 @ off 0
            clock.anchor(rtp_timestamp=0, utc=900_000_000.0)
            # Fill 20 s of audio as 0.5 s chunks at contiguous offsets.
            for i in range(40):
                ring.push(np.ones(6000, dtype=np.float32), start_offset=i * 6000)
            # latest RTP = just past newest sample (240000 samples in)
            box["rtp"] = clock.rtp_of_offset(40 * 6000)   # 240000 > 180000+settle

            worker._tick()
            worker._reap_all(wait=True)

            wavs = list((Path(tmpdir) / "msk144").glob("*.wav"))
            self.assertGreaterEqual(len(wavs), 1, "expected a WAV for slot0")
            # filename labels the epoch-aligned slot start (UTC 900000000)
            self.assertTrue(any("_28130.wav" in w.name for w in wavs))

    def test_tick_noop_before_anchor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(tmpdir)
            box["rtp"] = 123456            # latest set, but clock not anchored
            worker._tick()                 # must not raise / must do nothing
            self.assertEqual(
                list((Path(tmpdir) / "msk144").glob("*.wav")), [])

    def test_incomplete_slot_not_harvested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(tmpdir, keep_wav=True)
            clock.anchor(rtp_timestamp=0, utc=900_000_000.0)
            # only 10 s of data — slot0 (15 s) + settle not yet complete
            for i in range(20):
                ring.push(np.ones(6000, dtype=np.float32), start_offset=i * 6000)
            box["rtp"] = clock.rtp_of_offset(20 * 6000)   # 120000 < 198000
            worker._tick()
            self.assertEqual(
                list((Path(tmpdir) / "msk144").glob("*.wav")), [])


    def test_slot_window_follows_radiod_slide(self):
        """The extracted RTP window must track radiod's RTP↔UTC slide: for the
        SAME clean UTC boundary, a +1 s shift in the anchor's current UTC moves
        the window 1 s (12000 samples) earlier — i.e. we follow radiod instead
        of freezing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(
                tmpdir, keep_wav=True, decoder_path="/nonexistent/decode_msk144")
            clock.anchor(rtp_timestamp=0, utc=900_000_000.0)
            for i in range(80):                       # 40 s of audio
                ring.push(np.ones(6000, dtype=np.float32), start_offset=i * 6000)
            box["rtp"] = clock.rtp_of_offset(80 * 6000)   # latest_off = 480000
            captured: list[int] = []
            real = ring.extract_by_offset
            ring.extract_by_offset = lambda off, n: (captured.append(off)
                                                     or real(off, n))
            # No slide: boundary 900000015 → start_off (15 s)*12000 = 180000.
            worker._next_boundary_utc = 900_000_015.0
            box["anchor_utc"] = 900_000_000.0
            worker._tick()
            self.assertIn(180000, captured)
            # radiod's mapping of anchor_rtp slid +1.0 s → window 12000 earlier.
            captured.clear()
            worker._next_boundary_utc = 900_000_015.0
            box["anchor_utc"] = 900_000_001.0
            worker._tick()
            self.assertIn(168000, captured)


class DecodeTimeoutTests(unittest.TestCase):
    """A hung jt9 must be killed + reaped, not leaked forever.

    Regression for the unbounded _pending_procs / FD / WAV leak that
    otherwise grows until the MemoryMax cgroup OOM-kills the daemon.
    """

    def _worker(self, tmpdir, keep_wav=False):
        worker, _clock, _ring, _box = _make_worker(tmpdir, keep_wav=keep_wav)
        return worker

    def test_hung_decode_killed_past_deadline(self):
        import os
        import subprocess
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            worker = self._worker(tmpdir)
            wav = Path(tmpdir) / "hung.wav"
            wav.write_bytes(b"RIFFfake")
            proc = subprocess.Popen(
                ["sleep", "300"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.addCleanup(SlotWorker._kill_proc, proc)
            fds_before = len(os.listdir("/proc/%d/fd" % os.getpid()))
            # 5-tuple: (proc, wav_path, slot_start, fork_monotonic, prev_lines).
            # fork timestamp far enough in the past to be over any deadline.
            worker._pending_procs.append(
                (proc, wav, 0.0, time.monotonic() - 10_000, 0)
            )
            worker._reap_finished()
            time.sleep(0.2)
            self.assertIsNotNone(proc.poll(), "hung proc was not killed")
            self.assertLess(proc.returncode, 0, "expected death by signal")
            self.assertEqual(len(worker._pending_procs), 0, "not dropped from pending")
            self.assertEqual(worker.decodes_fail, 1)
            self.assertFalse(wav.exists(), "spool wav not cleaned up")
            fds_after = len(os.listdir("/proc/%d/fd" % os.getpid()))
            self.assertLessEqual(fds_after, fds_before, "FD leak on kill path")

    def test_in_deadline_decode_left_pending(self):
        import subprocess
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            worker = self._worker(tmpdir, keep_wav=True)
            wav = Path(tmpdir) / "young.wav"
            wav.write_bytes(b"RIFFfake")
            proc = subprocess.Popen(
                ["sleep", "300"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.addCleanup(SlotWorker._kill_proc, proc)
            worker._pending_procs.append((proc, wav, 0.0, time.monotonic(), 0))
            worker._reap_finished()
            self.assertIsNone(proc.poll(), "in-deadline proc wrongly killed")
            self.assertEqual(len(worker._pending_procs), 1, "in-deadline proc dropped")
            self.assertEqual(worker.decodes_fail, 0, "false failure counted")


if __name__ == "__main__":
    unittest.main()
