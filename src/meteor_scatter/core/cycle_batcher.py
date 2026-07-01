"""MeteorScatterCycleBatcher: per-cycle, per-source spot accumulator.

Sits between the ChTailers (one per radiod-mode) and the SQLite sink.
Accepts decoded spots from any thread; flushes once per (cycle, source)
on a dedicated writer thread.  Same shape as wspr-recorder's
``CycleBatcher`` so multi-rx behavior is uniform across clients.

Why the indirection:

  * **Cycle-aligned log line.**  Emits a single ``cycle UTC HH:MM:SS
    rx=<rx> → N spots in psk.spots ...`` line per (cycle, source) so
    ``smd watch msk144`` can render per-receiver activity the same way
    ``smd watch wspr`` does, even when cross-rx dedup deletes
    "loser" siblings before a downstream observer can read them.
  * **Foundation for cross-rx dedup (Phase D).**  Best-of-N picks
    per ``(band, callsign, cycle)`` require seeing all receivers'
    spots for the same cycle in one place.
  * **Thread-affinity discipline.**  ``sqlite3.Connection`` is bound
    to the thread that opened it.  MeteorScatterRecorder may eventually
    parallelise tail processing across sources; centralising the
    writer in this batcher's own thread keeps a single SQLite
    connection irrespective of how many tailers feed it.

Cycle boundaries:

  * MSK144 — 15-second T/R cycles aligned to UTC seconds 0/15/30/45.

A spot's ``time`` field (from the decoder) is floored to its cycle
boundary to compute ``cycle_start_iso``.  Batches are keyed by
``((mode, cycle_start_iso), rx_source)`` — the empty rx_source key
preserves single-source behaviour exactly.

The flush deadline is env-overridable:

  * ``METEOR_SCATTER_CYCLE_DEADLINE_SEC``  default 10.0 s

The deadline is the wall-clock window after the FIRST spot lands in
a batch before the batch flushes — short enough that latency to
wsprdaemon.org stays within a single cycle plus deadline, long enough
to give every source's tailer time to surface its own decodes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


def _supervise(name, alive, fn, *args):
    """Run a background-thread loop, converting a silent thread death into a
    loud log + backed-off auto-restart.

    These loops already guard their expected per-iteration errors inline, so
    an exception reaching here is unexpected -- and a bare daemon thread that
    dies takes its subsystem (spot batching / channel-lifetime refresh /
    stats) down silently, with no operator signal and (for the batcher) an
    unbounded _batches backlog.  Re-invoke the loop after a capped backoff
    while the daemon is still running.  ``alive`` is a predicate (e.g.
    ``lambda: self._running``); ``fn`` returns normally only on a stop.
    """
    backoff = 1.0
    while alive():
        try:
            fn(*args)
            return
        except Exception:
            logger.exception("%s thread crashed unexpectedly", name)
            if not alive():
                return
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)
            logger.warning("%s thread restarting after crash", name)


# MSK144 T/R cycle length.  Spec-level constant — never override; the
# deadline (post-cycle wait window) is the operator-tunable knob, not this.
MSK144_CYCLE_SEC = 15.0


# Canonical band names by MSK144 standard dial frequency.  Used only
# for the log-line breakdown — the row's ``frequency`` field stays as
# absolute Hz everywhere else.  Frequencies that aren't in the table
# render under their kHz value (so an off-band exotic decode still
# shows up; the operator just sees an unfamiliar tag).
#
# MSK144 dial frequencies per WSJT-X:
#   https://www.physics.princeton.edu/pulsar/k1jt/wsjtx.html
_BAND_NAMES: dict[int, str] = {
    28130000: "10",
    50260000: "6",
}


def _freq_to_band_name(freq_hz: int) -> str:
    """Map a row's centre frequency to a canonical band name.

    Two-tier lookup: exact match in ``_BAND_NAMES`` first (the MSK144
    10 m / 6 m dials), then nearest-100 kHz bucket so non-standard
    tuning still groups sensibly under one tag.
    """
    if freq_hz in _BAND_NAMES:
        return _BAND_NAMES[freq_hz]
    # Round to nearest 100 kHz; produces e.g. "14075k" for an oddly-
    # tuned channel near 20 m.  Kept compact so the log line stays
    # readable.
    bucket_khz = (freq_hz + 50_000) // 100_000 * 100
    return f"{bucket_khz}k"


def _resolve_deadline_sec(env_name: str, default: float) -> float:
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        v = float(raw)
        if v <= 0:
            raise ValueError("must be > 0")
        return v
    except (ValueError, TypeError):
        logger.warning(
            "meteor-scatter: ignoring invalid %s=%r (using default %.1f s)",
            env_name, raw, default,
        )
        return default


def _cycle_start(ts: datetime, mode: str) -> datetime:
    """Floor ``ts`` to the start of its MSK144 T/R cycle.

    ``mode`` is accepted for signature compatibility with the multi-mode
    skeleton; this client only ever emits ``msk144`` (15 s cycles).  An
    unexpected tag falls back to the same 15 s grid rather than crashing.
    """
    cycle_sec = MSK144_CYCLE_SEC
    # Convert to UTC seconds-since-epoch (float), floor, project back.
    epoch = ts.timestamp()
    floored = (int(epoch / cycle_sec)) * cycle_sec
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def _cycle_iso(start: datetime) -> str:
    """Compact ISO timestamp for the cycle's start.

    MSK144's 15 s cycles always land on an integer second, so the
    one-decimal-place suffix is always ``.0`` — kept for format parity
    with the wspr/psk watch parsers and lexicographic sortability.
    """
    return start.strftime("%Y-%m-%dT%H:%M:%S.") + f"{start.microsecond // 100_000}Z"


class _MeteorScatterCycleBatch:
    """One (mode, cycle_start, rx_source) batch awaiting flush.

    Lives entirely under :class:`MeteorScatterCycleBatcher`'s lock; not thread-
    safe on its own.
    """

    __slots__ = (
        "cycle_key", "rx_source", "radiod_id",
        "deadline", "rows", "band_counts",
    )

    def __init__(
        self,
        cycle_key: tuple[str, str],
        rx_source: str,
        radiod_id: str,
        deadline: float,
    ) -> None:
        self.cycle_key = cycle_key   # (mode, cycle_start_iso)
        self.rx_source = rx_source
        self.radiod_id = radiod_id
        self.deadline = deadline
        self.rows: list[dict] = []
        # band_name -> spot count, populated as rows are added so the
        # flush log line doesn't have to re-iterate the row list.
        self.band_counts: dict[str, int] = {}

    def add(self, row: dict) -> None:
        self.rows.append(row)
        band = _freq_to_band_name(int(row.get("frequency") or 0))
        self.band_counts[band] = self.band_counts.get(band, 0) + 1


class MeteorScatterCycleBatcher:
    """Per-cycle, per-source SQLite gateway for MSK144 spots.

    Single instance per :class:`MeteorScatterRecorder` process; all
    :class:`~meteor_scatter.core.ch_tailer.ChTailer` instances feed it
    via :meth:`add`.

    Usage::

        batcher = MeteorScatterCycleBatcher(writer_factory=_default_writer_factory)
        batcher.start()
        # ... from each tailer thread:
        batcher.add(rows, rx_source="radiod:bee1-status.local",
                    radiod_id="bee1")
        # On shutdown:
        batcher.stop()
    """

    def __init__(
        self,
        writer_factory: Callable[[int], Any],
        *,
        cycle_deadline_sec: Optional[float] = None,
        batch_rows: int = 200,
    ) -> None:
        if cycle_deadline_sec is None:
            cycle_deadline_sec = _resolve_deadline_sec(
                "METEOR_SCATTER_CYCLE_DEADLINE_SEC", 10.0,
            )
        self._cycle_deadline_sec = float(cycle_deadline_sec)
        self._writer_factory = writer_factory
        self._batch_rows = batch_rows
        self._writer = None

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        # Batches keyed by ((mode, cycle_iso), rx_source).
        self._batches: dict[tuple[tuple[str, str], str], _MeteorScatterCycleBatch] = {}
        self._stop = threading.Event()
        self._wake_callback: Optional[Callable[[], None]] = None
        self._thread: Optional[threading.Thread] = None

    # --- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=lambda: _supervise(
                "msk144-cycle-batcher", lambda: not self._stop.is_set(),
                self._run,
            ),
            name="msk144-cycle-batcher", daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=timeout)
        self._thread = None

    def set_wake_callback(
        self, callback: Optional[Callable[[], None]],
    ) -> None:
        """Register (or clear) a wake callback fired after each commit.

        :class:`MeteorScatterRecorder` wires this to the in-process uploader so
        a cycle commit nudges the uploader to pump immediately
        without waiting for its next poll tick.
        """
        self._wake_callback = callback

    # --- producer surface ---------------------------------------------

    def add(
        self,
        rows: Iterable[dict],
        *,
        rx_source: str,
        radiod_id: str,
    ) -> None:
        """Enqueue ``rows`` for their respective cycle batches.

        Cheap: appends to dicts under a mutex; no I/O.  The writer
        thread picks each batch up at its per-mode deadline.  Empty
        ``rows`` is a no-op.

        Rows are partitioned by ``(mode, cycle_start_iso, rx_source)``
        so a tailer that emits a mix of FT8 + FT4 lines (same log
        file in some configurations) is split correctly.
        """
        rows = list(rows)
        if not rows:
            return
        now = time.monotonic()
        with self._cond:
            for row in rows:
                mode = (row.get("mode") or "").lower()
                ts = row.get("time")
                if not isinstance(ts, datetime):
                    # No usable timestamp — write through without
                    # batching so we don't lose the row to a bad
                    # parse.  Drop into the unlocked path below.
                    self._writer_insert_passthrough(row)
                    continue
                start = _cycle_start(ts, mode)
                cycle_key = (mode, _cycle_iso(start))
                key = (cycle_key, rx_source)
                batch = self._batches.get(key)
                if batch is None:
                    deadline = now + self._cycle_deadline_sec
                    batch = _MeteorScatterCycleBatch(
                        cycle_key=cycle_key,
                        rx_source=rx_source,
                        radiod_id=radiod_id,
                        deadline=deadline,
                    )
                    self._batches[key] = batch
                batch.add(row)
            self._cond.notify()

    def _writer_insert_passthrough(self, row: dict) -> None:
        """Best-effort write of a row with no usable cycle anchor.

        Called with the cond lock held.  We can't dispatch via the
        writer thread cleanly here because the writer may not yet
        exist; log and drop so a malformed row doesn't poison the
        rest of the cycle.  Practically never hit — every parser in
        ch_tailer sets ``time`` to a tz-aware datetime.
        """
        logger.warning(
            "msk144-cycle-batcher: dropping row with no usable time "
            "(mode=%r): %s",
            row.get("mode"), row.get("message", "<no message>"),
        )

    # --- writer thread -------------------------------------------------

    def _run(self) -> None:
        """Writer loop — single thread that owns the SQLite Writer."""
        try:
            self._writer = self._writer_factory(self._batch_rows)
        except Exception:
            logger.exception(
                "msk144-cycle-batcher: writer factory raised — batcher "
                "disabled, spots will not reach psk.spots",
            )
            return

        while not self._stop.is_set():
            ready: list[_MeteorScatterCycleBatch] = []
            with self._cond:
                while not self._stop.is_set():
                    now = time.monotonic()
                    next_deadline = None
                    for key, batch in list(self._batches.items()):
                        if batch.deadline <= now:
                            ready.append(batch)
                            del self._batches[key]
                        elif (
                            next_deadline is None
                            or batch.deadline < next_deadline
                        ):
                            next_deadline = batch.deadline
                    if ready:
                        break
                    wait = (
                        max(0.05, next_deadline - now)
                        if next_deadline is not None else 1.0
                    )
                    self._cond.wait(timeout=wait)
                if self._stop.is_set():
                    break

            for batch in ready:
                self._flush(batch)

        # Drain any remaining batches on stop — make a best effort to
        # not lose spots already accepted.
        with self._cond:
            remaining = list(self._batches.values())
            self._batches.clear()
        for batch in remaining:
            self._flush(batch)
        try:
            if self._writer is not None:
                self._writer.close()
        except Exception:
            logger.exception("msk144-cycle-batcher: writer close failed")

    def _flush(self, batch: _MeteorScatterCycleBatch) -> None:
        """Write one batch to SQLite + emit the per-rx cycle log line.

        Failures are logged and swallowed — one bad batch must not
        kill the writer thread.  PSKReporter delivery is downstream
        of the SQLite sink, so the next cycle's spots still flow.
        """
        n = len(batch.rows)
        if n == 0:
            return
        wall_start = time.monotonic()
        try:
            self._writer.insert(batch.rows)
            self._writer.flush()
        except Exception:
            logger.exception(
                "msk144-cycle-batcher: insert failed for "
                "cycle=%s rx=%s (%d rows)",
                batch.cycle_key, batch.rx_source, n,
            )
            return
        elapsed_ms = int((time.monotonic() - wall_start) * 1000)

        mode, cycle_iso = batch.cycle_key
        # ``smd watch msk144`` parses this format; mirror wspr-recorder's
        # cycle log shape so the watch parser can be reused with a
        # near-identical regex.  Band counts are emitted unsorted —
        # the watch formatter handles ordering.
        bands_breakdown = " ".join(
            f"{band}:{count}"
            for band, count in batch.band_counts.items()
        )
        rx_label = batch.rx_source or batch.radiod_id or "?"
        logger.info(
            "cycle UTC %s rx=%s mode=%s → %d spots in psk.spots "
            "(sqlite write %d ms)%s",
            cycle_iso, rx_label, mode, n, elapsed_ms,
            f" bands=[{bands_breakdown}]" if bands_breakdown else "",
        )

        cb = self._wake_callback
        if cb is not None:
            try:
                cb()
            except Exception:
                logger.exception(
                    "msk144-cycle-batcher: wake callback raised",
                )

    # --- introspection (mostly for tests) ------------------------------

    @property
    def writer(self):
        """Underlying Writer; None until the writer thread has
        constructed it.  Tests use this to inject expectations."""
        return self._writer

    def pending_batches(self) -> int:
        """Count of batches not yet flushed — for tests / diagnostics."""
        with self._cond:
            return len(self._batches)
