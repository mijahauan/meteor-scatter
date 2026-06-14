"""Tests for ChannelSink's anchor-once model.

The recorder reads wall clock ONCE on the first batch (via
rtp_to_wallclock when channel_info is available, else time.time()
fallback), saves an `_anchor_utc` + `_anchor_total_samples` pair,
and projects every subsequent batch's UTC by pure sample-count
arithmetic.  No further wall-clock reads are used for timing.

These tests use a fake Ring + SlotWorker via monkey-patching so we
can drive on_samples() in isolation.
"""

import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import numpy as np

from msk144_recorder.core.stream import ChannelSink


@dataclass
class _FakeQuality:
    """Stand-in for MultiStream's StreamQuality."""
    total_samples_delivered: int = 0
    first_rtp_timestamp: int = 0


class _FakeChannelInfo:
    """Stand-in for ka9q's ChannelInfo carrying gps_time / rtp_timesnap."""
    def __init__(self, gps_time=1462564880_000_000_000, rtp_timesnap=1):
        self.gps_time = gps_time
        self.rtp_timesnap = rtp_timesnap


def _make_sink() -> ChannelSink:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "msk144").mkdir(exist_ok=True)
    log_fd = open(tmp / "log", "ab")
    sink = ChannelSink(
        mode="msk144",
        frequency_hz=28_130_000,
        sample_rate=12_000,
        preset="usb",
        encoding=0,
        spool_dir=tmp,
        log_fd=log_fd,
        decoder_path="/nonexistent",
        keep_wav=False,
        authority_reader=None,
    )
    sink._tmp_dir = tmp  # type: ignore[attr-defined]
    return sink


def _cleanup_sink(sink) -> None:
    tmp = getattr(sink, "_tmp_dir", None)
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


class TestAnchorOnce(unittest.TestCase):

    def test_first_batch_anchors_from_wall_clock_fallback(self):
        """No channel_info → time.time()-based anchor on first batch."""
        sink = _make_sink()
        try:
            samples = np.zeros(2400, dtype=np.float32)   # 200 ms at 12 kHz
            q = _FakeQuality(total_samples_delivered=2400, first_rtp_timestamp=0)
            with mock.patch("msk144_recorder.core.stream.time.time",
                            return_value=1_700_000_000.0):
                with mock.patch.object(sink._ring, "push") as push:
                    sink.on_samples(samples, q)
            self.assertEqual(sink._anchor_source, "wallclock_fallback")
            # First-sample UTC = wall_now - n/sample_rate = 1700000000.0 - 0.2
            self.assertAlmostEqual(sink._anchor_utc, 1_699_999_999.8, places=3)
            self.assertEqual(sink._anchor_total_samples, 0)
            # Ring received the projected UTC for the first sample.
            push.assert_called_once()
            self.assertAlmostEqual(push.call_args[0][1],
                                   1_699_999_999.8, places=3)
        finally:
            _cleanup_sink(sink)

    def test_first_batch_anchors_via_rtp_to_wallclock_when_available(self):
        """channel_info set + rtp_to_wallclock returns a number → use it."""
        sink = _make_sink()
        sink.set_channel_info(_FakeChannelInfo())
        try:
            samples = np.zeros(2400, dtype=np.float32)
            q = _FakeQuality(total_samples_delivered=2400,
                             first_rtp_timestamp=1_000_000)
            with mock.patch("ka9q.rtp_to_wallclock",
                            return_value=1_700_000_500.0):
                with mock.patch("msk144_recorder.core.stream.time.time",
                                return_value=1_700_000_500.0):
                    with mock.patch.object(sink._ring, "push"):
                        sink.on_samples(samples, q)
            self.assertEqual(sink._anchor_source, "rtp_to_wallclock")
            self.assertAlmostEqual(sink._anchor_utc, 1_700_000_500.0, places=3)
        finally:
            _cleanup_sink(sink)

    def test_authority_offset_is_applied_to_anchor(self):
        """A usable hf-timestd §18 offset is ADDED to the rtp_to_wallclock
        anchor (S2), and the source records that it was applied."""
        class _FakeSnap:
            offset_usable = True
            offset_seconds = 0.004250  # 4.25 ms
        class _FakeReader:
            def read(self):
                return _FakeSnap()

        tmp = Path(tempfile.mkdtemp())
        (tmp / "msk144").mkdir(exist_ok=True)
        log_fd = open(tmp / "log", "ab")
        sink = ChannelSink(
            mode="msk144", frequency_hz=28_130_000, sample_rate=12_000,
            preset="usb", encoding=0, spool_dir=tmp, log_fd=log_fd,
            decoder_path="/nonexistent", keep_wav=False,
            authority_reader=_FakeReader(),
        )
        sink.set_channel_info(_FakeChannelInfo())
        try:
            samples = np.zeros(2400, dtype=np.float32)
            q = _FakeQuality(total_samples_delivered=2400,
                             first_rtp_timestamp=1_000_000)
            with mock.patch("ka9q.rtp_to_wallclock",
                            return_value=1_700_000_500.0):
                with mock.patch("msk144_recorder.core.stream.time.time",
                                return_value=1_700_000_500.0):
                    with mock.patch.object(sink._ring, "push"):
                        sink.on_samples(samples, q)
            self.assertEqual(sink._anchor_source, "rtp_to_wallclock+authority")
            self.assertAlmostEqual(sink._anchor_utc, 1_700_000_500.00425, places=4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_subsequent_batches_use_pure_sample_count_projection(self):
        """Second batch's UTC = anchor + delivered_since_anchor/sample_rate.

        Critically: time.time() is moved during the test, but the
        projection IGNORES that — confirms zero wall-clock dependency
        after anchor."""
        sink = _make_sink()
        try:
            sr = sink.sample_rate
            with mock.patch.object(sink._ring, "push") as push:
                # Anchor at wall_now=1000.0 with 1 sec of samples.
                with mock.patch("msk144_recorder.core.stream.time.time",
                                return_value=1000.0):
                    sink.on_samples(
                        np.zeros(sr, dtype=np.float32),
                        _FakeQuality(total_samples_delivered=sr),
                    )

                # Now ANY value of time.time() must not affect the
                # projection — push another 0.5 s of samples and verify
                # we see anchor + 1.0 s (since anchor accounted for the
                # FIRST 1 s and the new batch's first sample is at +1.0 s).
                with mock.patch("msk144_recorder.core.stream.time.time",
                                return_value=999_999_999.0):  # absurd
                    sink.on_samples(
                        np.zeros(sr // 2, dtype=np.float32),
                        _FakeQuality(total_samples_delivered=sr + sr // 2),
                    )

            # First push: utc_of_first = anchor (1000.0 - 1.0) = 999.0
            self.assertAlmostEqual(push.call_args_list[0][0][1], 999.0, places=4)
            # Second push: anchor + (sr / sr) = anchor + 1.0 = 1000.0
            self.assertAlmostEqual(push.call_args_list[1][0][1], 1000.0, places=4)
        finally:
            _cleanup_sink(sink)

    def test_on_stream_restored_re_anchors(self):
        """Stream-gap recovery MUST re-anchor.  This inverts the older
        contract — see commit 502573c (``fix(stream): re-anchor on
        stream_restored after radiod outage``) and the design comment
        in stream.py::on_stream_restored.

        Background: MultiStream only fires on_stream_restored after a
        real radiod restart (15 s + ensure_channel success), and on
        such a restart MultiStream resets ``quality =
        StreamQuality()`` so ``total_samples_delivered`` restarts at 0.
        Holding the pre-restart anchor across that discontinuity drives
        ``delta_samples`` wildly negative, every projected UTC misses
        every slot window, and decodes silently fall to 0/0 forever
        (observed B4-100 2026-05-14, every band silent 3 h).

        Correct behavior after restore:
          - ``_anchor_utc`` cleared to None
          - ``_anchor_total_samples`` cleared to 0
          - ``_channel_info`` REPLACED by the new snapshot
        The next ``on_samples`` call re-anchors from a fresh wall-clock
        correlation.
        """
        sink = _make_sink()
        sink.set_channel_info(_FakeChannelInfo(rtp_timesnap=1))
        try:
            sr = sink.sample_rate
            with mock.patch.object(sink._ring, "push"):
                with mock.patch("ka9q.rtp_to_wallclock",
                                return_value=2000.0):
                    sink.on_samples(
                        np.zeros(sr, dtype=np.float32),
                        _FakeQuality(total_samples_delivered=sr,
                                     first_rtp_timestamp=1),
                    )
            # First batch anchored normally.
            self.assertEqual(sink._anchor_utc, 2000.0)
            self.assertIsNotNone(sink._anchor_source)

            new_info = _FakeChannelInfo(rtp_timesnap=999)
            sink.on_stream_restored(new_info)

            # Anchor MUST be cleared so the next batch re-anchors.
            self.assertIsNone(sink._anchor_utc)
            self.assertEqual(sink._anchor_total_samples, 0)
            # channel_info MUST be replaced by the new snapshot.
            self.assertIs(sink._channel_info, new_info)
        finally:
            _cleanup_sink(sink)


if __name__ == "__main__":
    unittest.main()
