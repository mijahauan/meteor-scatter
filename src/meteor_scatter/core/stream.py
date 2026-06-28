"""ChannelSink: per-channel Ring + SlotWorker driven by MultiStream callbacks.

One ChannelSink per (mode, frequency). The sink owns no socket and no
thread of its own for RTP reception — it receives sample batches via
the `on_samples` callback that a shared `MultiStream` dispatches after
demultiplexing by SSRC.

Timing model (RTP-referenced ka9q.SlotClock — anchor ONCE, defer to RTP).

  1. On the FIRST on_samples batch we anchor a shared ``ka9q.SlotClock``
     off radiod's GPS-true RTP timestamp (``quality.last_rtp_timestamp``)
     mapped to UTC via the suite-shared ``hamsci_dsp.timing.acquire_anchor_utc``
     helper.  Preferred source: ``ka9q.rtp_to_utc`` (radiod's GPS_TIME /
     RTP_TIMESNAP snapshot) plus hf-timestd's §18 dynamic RTP→UTC offset.
     Fallback when channel_info is unavailable: the host wall clock.

  2. Every subsequent batch is pushed to the ring keyed by its absolute
     RTP **sample offset** (``clock.offset_of_rtp(batch_first_rtp)``) —
     NOT by a delivered-sample-count UTC projection.  The audio handed to
     jt9 therefore always lines up with the RTP grid point its WAV is
     labelled with, which removes the long-standing "decodes=N/N but
     spots=0" drift surface entirely.

  3. We re-anchor ONLY on a genuine stream restart (``on_stream_restored``,
     fired by MultiStream after a real radiod outage).  We deliberately do
     NOT second-guess the grid by re-reading radiod's status feed per batch:
     the grid is RTP-driven and drift-immune, so we defer to radiod's RTP.

Per METROLOGY.md §4.5 RTP-reference invariant, the recorder does not
diagnose timing health on its own — that is hf-timestd's job.  If the
host clock is badly wrong at anchor time, decode rate goes to zero and
the operator sees the symptom through the standard decode-health signal
(decodes_ok/decodes_total + sigmond's wav_snapshot), not through any
client-side wall-clock comparison.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from ka9q import SlotClock

from meteor_scatter.config import MSK144_CADENCE_SEC
from hamsci_dsp.timing import AuthorityReader
from meteor_scatter.core.ring import Ring
from meteor_scatter.core.slot import SlotWorker, SETTLE_SEC

logger = logging.getLogger(__name__)

RING_SECONDS = 60.0


class ChannelSink:
    """Ring + SlotWorker for one channel, fed by MultiStream callbacks."""

    def __init__(
        self,
        mode: str,
        frequency_hz: int,
        sample_rate: int,
        preset: str,
        encoding: int,
        spool_dir: Path,
        log_fd,
        decoder_path: str,
        keep_wav: bool = False,
        authority_reader: Optional[AuthorityReader] = None,
        decoder_kind: str = "jt9",
        spool_spots: bool = False,
    ):
        self._mode = mode
        self._frequency_hz = frequency_hz
        self._sample_rate = sample_rate
        self._preset = preset
        self._encoding = encoding

        cadence = MSK144_CADENCE_SEC

        self._ring = Ring(
            max_seconds=RING_SECONDS,
            sample_rate=sample_rate,
        )

        # Epoch-aligned, RTP-referenced slot timing.  The clock is anchored
        # off radiod's GPS-true RTP timestamp (on_samples) and harvested by
        # the SlotWorker thread; the lock guards the shared clock state.
        self._clock = SlotClock(
            cadence_sec=cadence, sample_rate=sample_rate, settle_sec=SETTLE_SEC,
        )
        self._clock_lock = threading.Lock()
        # RTP timestamp just past the newest delivered sample (the clock's
        # high-water mark).  Written by on_samples, read by the SlotWorker.
        self._latest_rtp: Optional[int] = None

        self._slot_worker = SlotWorker(
            ring=self._ring,
            mode=mode,
            frequency_hz=frequency_hz,
            cadence_sec=cadence,
            spool_dir=spool_dir / mode,
            log_fd=log_fd,
            decoder_path=decoder_path,
            clock=self._clock,
            get_latest_rtp=lambda: self._latest_rtp,
            clock_lock=self._clock_lock,
            get_anchor_utc_now=self._anchor_utc_now,
            decoder_kind=decoder_kind,
            keep_wav=keep_wav,
            spool_spots=spool_spots,
        )

        self._total_delivered: int = 0
        # ChannelInfo carrying gps_time / rtp_timesnap / chain_delay — used to
        # map RTP→UTC ONCE at anchor time (and again only at a genuine stream
        # restart, via on_stream_restored).  We do NOT re-read it per batch to
        # second-guess the grid: SlotClock's grid is RTP-driven and drift-
        # immune, so we defer to radiod's RTP and never re-anchor on status
        # jitter.
        self._channel_info = None
        # Diagnostic: how the current SlotClock anchor was derived.
        self._anchor_source: str = ""        # "rtp_to_utc[+authority]" | "wallclock_fallback"
        # The fixed RTP reference the ring + grid are keyed to (set once at the
        # first anchor; reset only on a genuine stream restart).
        self._anchor_rtp: Optional[int] = None
        # §18 authority reader — supplies the dynamic RTP→UTC offset.
        self._reader = authority_reader if authority_reader is not None else AuthorityReader()

    def _anchor_utc_now(self) -> Optional[float]:
        """Current UTC of the FIXED ``_anchor_rtp`` per radiod's live
        ``rtp_to_utc`` + §18 authority offset, or None if not yet anchored /
        no channel_info.

        This is the slide-follow hook.  ``_anchor_rtp`` never moves (so the
        ring stays valid), but radiod's RTP↔UTC mapping slowly slides; the
        SlotWorker calls this each tick and re-pins every slot's RTP window to
        the *current* mapping, so the windows track the slide instead of
        freezing.  It is a smooth, sub-sample nudge — NOT the per-batch
        compare-and-flush re-anchor that stormed.
        """
        if self._anchor_rtp is None or self._channel_info is None:
            return None
        from ka9q import rtp_to_utc
        from hamsci_dsp.timing import acquire_anchor_utc
        a = acquire_anchor_utc(
            first_rtp=self._anchor_rtp,
            channel_info=self._channel_info,
            rtp_to_utc=rtp_to_utc,
            authority_reader=self._reader,
            sample_rate=self._sample_rate,
        )
        return a.utc if a.rtp_referenced else None

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def frequency_hz(self) -> int:
        return self._frequency_hz

    def stats_snapshot(self) -> dict:
        sw = self._slot_worker
        return {
            "mode": self._mode,
            "freq": self._frequency_hz,
            "decodes_ok": sw.decodes_ok,
            "decodes_fail": sw.decodes_fail,
            "slots_empty": sw.slots_empty,
        }

    def start(self) -> None:
        self._slot_worker.start()
        logger.info(
            "%s %d Hz: sink started (sr=%d)",
            self._mode.upper(), self._frequency_hz, self._sample_rate,
        )

    def stop(self) -> None:
        self._slot_worker.stop()
        logger.info(
            "%s %d Hz: sink stopped (total_delivered=%d)",
            self._mode.upper(), self._frequency_hz, self._total_delivered,
        )

    def set_channel_info(self, channel_info) -> None:
        """Attach the ChannelInfo carrying gps_time/rtp_timesnap/chain_delay.

        Called by the recorder right after multi.add_channel() returns.
        Without it, on_samples falls back to wall-clock anchoring (the
        old broken path) and logs a one-time warning per channel.
        """
        self._channel_info = channel_info

    def on_samples(self, samples: np.ndarray, quality) -> None:
        """MultiStream callback — feed the RTP-referenced SlotClock + ring.

        Each batch is tagged with the absolute sample offset of its FIRST
        sample, derived from radiod's GPS-true RTP timestamp
        (``quality.last_rtp_timestamp``) via the shared ``ka9q.SlotClock`` —
        NOT from a delivered-sample-count projection.  This is the fix for
        the long-standing "decodes=N/N but spots=0" drift (the audio handed
        to the decoder now always lines up with the RTP grid point its WAV
        is labelled with).  The slot harvesting + WAV write happen on the
        SlotWorker thread; here we only anchor (once) and push.
        """
        n = len(samples)
        if n == 0:
            return
        last_rtp = getattr(quality, "last_rtp_timestamp", None)
        if not last_rtp:
            # No RTP timestamp yet (pre-first-packet) — nothing to anchor to.
            return
        last_rtp = int(last_rtp) & 0xFFFFFFFF
        # RTP timestamp of this batch's first sample.  The batch ends ~at the
        # last packet's timestamp; first sample = last_rtp - n.  The <1-packet
        # constant bias from ignoring the final packet's own length is
        # harmless (jt9 --msk144 tolerates the slot's settle window) and does
        # NOT accumulate — every batch is pinned to a true GPS-stamped RTP
        # value.
        batch_first_rtp = (last_rtp - n) & 0xFFFFFFFF

        with self._clock_lock:
            if not self._clock.anchored:
                anchor_utc, source = self._anchor_utc_for(batch_first_rtp, n)
                if anchor_utc is None:
                    return
                self._clock.anchor(batch_first_rtp, anchor_utc)
                self._anchor_source = source
                # The fixed RTP reference for the ring + the slide-follow
                # re-pin (see _anchor_utc_now).  Set once; only changes on a
                # genuine stream restart (on_stream_restored resets the clock).
                self._anchor_rtp = batch_first_rtp
                logger.info(
                    "%s %d Hz: SlotClock anchored via %s",
                    self._mode.upper(), self._frequency_hz, source,
                )
            start_off = self._clock.offset_of_rtp(batch_first_rtp)

        self._ring.push(samples, start_off)
        self._latest_rtp = last_rtp
        self._total_delivered += n

    def _anchor_utc_for(self, rtp_ts: int, n: int):
        """Return (utc, source) mapping ``rtp_ts`` -> UTC via the suite-shared
        anchor helper.

        Preferred: radiod's GPS/RTP timebase (``ka9q.rtp_to_utc``) plus the
        hf-timestd §18 dynamic RTP→UTC offset.  Fallback: the host wall clock
        naming this batch's first sample (``n`` samples back).  The logic lives
        once in ``hamsci_dsp.timing.acquire_anchor_utc`` so every sigmond
        recorder anchors identically — no per-client copies to drift.
        """
        from ka9q import rtp_to_utc
        from hamsci_dsp.timing import acquire_anchor_utc
        a = acquire_anchor_utc(
            first_rtp=rtp_ts,
            channel_info=self._channel_info,
            rtp_to_utc=rtp_to_utc,
            authority_reader=self._reader,
            samples_behind=n,
            sample_rate=self._sample_rate,
        )
        return a.utc, a.source

    def on_stream_dropped(self, reason: str) -> None:
        logger.warning(
            "%s %d Hz: stream dropped — %s",
            self._mode.upper(), self._frequency_hz, reason,
        )

    def on_stream_restored(self, channel_info) -> None:
        # Re-anchor on stream restoration.  MultiStream only fires this
        # callback after _drop_timeout_sec (default 15s) of silence AND
        # a successful ensure_channel() — i.e. a real radiod restart or
        # comparable outage, never a sub-second multicast hiccup.  On
        # such a restart, MultiStream resets ``slot.quality =
        # StreamQuality()``, so the RTP timestamps restart from radiod's
        # fresh epoch.  Holding the pre-restart anchor across that
        # discontinuity makes every projected offset miss every slot
        # window, and decodes silently fall to 0/0 forever (observed
        # B4-100 2026-05-14: radiod bounced, every band silent for 3 h
        # until manual stop+start).  Re-anchoring is the intended
        # behavior.
        self._channel_info = channel_info
        with self._clock_lock:
            self._clock.reset()
        self._ring.clear()
        self._latest_rtp = None
        self._anchor_source = ""
        self._anchor_rtp = None
        # New RTP reference space → the SlotWorker must re-seed its boundary.
        self._slot_worker.reset_boundary()
        logger.info(
            "%s %d Hz: stream restored — re-anchoring on next batch",
            self._mode.upper(), self._frequency_hz,
        )

    @property
    def preset(self) -> str:
        return self._preset

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def encoding(self) -> int:
        return self._encoding
