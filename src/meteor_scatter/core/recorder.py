"""MeteorScatterRecorder: orchestrates one or more radiod sources.

A single MeteorScatterRecorder process drives one ``ReceiverManager`` per
source (= one radiod control plane).  Legacy single-radiod
deployments pass a one-element list and behavior is unchanged.
Multi-source deployments pass several blocks and the same process
talks to a local radiod plus remote radiods over the LAN — mirrors
wspr-recorder's multi-source pattern.

MeteorScatterRecorder remains responsible for the process-global concerns
(chrony settle gate, spot deposit to the SQLite sink via the cycle
batcher, lifetime keepalive thread, stats aggregator, main loop,
watchdog, signal handling).  Per-radiod provisioning lives in
:class:`ReceiverManager`.  Decoded MSK144 spots are deposited into the
shared ``psk.spots`` sink (per-row ``mode="msk144"``) and delivered by the
single-host uploader's psk→PSKReporter pipeline (the same stream psk-recorder
uses; see :mod:`meteor_scatter.core.ch_tailer`).
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Optional, Union

from meteor_scatter.config import (
    derive_source_key,
    get_freqs,
    resolve_radiod_status,
)
# ChannelSink imports numpy via meteor_scatter.core.stream — kept under
# TYPE_CHECKING so this module imports cleanly in lightweight test
# environments without numpy.  The real instantiation lives in
# ReceiverManager.provision_channels which lazy-imports stream.
from typing import TYPE_CHECKING
if TYPE_CHECKING:  # pragma: no cover
    from meteor_scatter.core.stream import ChannelSink
from meteor_scatter.core.ch_tailer import ChTailer, _default_writer_factory
from meteor_scatter.core.cycle_batcher import MeteorScatterCycleBatcher
# MSK144 spots are attempted QSOs → uploaded to PSKReporter the same way
# FT4/FT8 are (psk-recorder's HsPskReporterUploader).  The ChTailer
# deposits rows into sigmond's local SQLite sink (psk.spots); the
# uploader pumps that queue to pskreporter.info via the hs-uploader
# PskReporterTcp transport (which maps mode "msk144" → "MSK144").
from meteor_scatter.core.hs_uploader_shim import HsPskReporterUploader
from meteor_scatter.core.receiver_manager import (
    ReceiverManager,
    _resolve_encoding,  # re-exported for any external importer
)

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


class _ProgressGate:
    """Decide whether to pet the systemd watchdog from a data-path progress
    signal, so a *wedged* (not crashed) daemon stops pinging and gets
    restarted while a healthy-but-idle one keeps pinging.

    ``update(progress, now)`` returns True to ping, False to withhold.
    Withholds only after the progress counter has stalled past ``stall_sec``;
    enforcement of that threshold begins only once progress has advanced at
    least once.  A separate, longer ``startup_grace_sec`` covers the
    never-progressed case (dead-from-start) without false-firing during slow
    provisioning.  ``progress is None`` means "unknown" -> always ping
    (fail-safe: uncertainty must never withhold the ping).
    """

    def __init__(self, stall_sec, startup_grace_sec=None):
        self._stall = stall_sec
        self._startup_grace = (
            startup_grace_sec if startup_grace_sec is not None
            else stall_sec * 2
        )
        self._last = None
        self._last_advance = None
        self._seen = False
        self._start = None

    def update(self, progress, now) -> bool:
        if self._start is None:
            self._start = now
        if progress is None:
            return True
        if self._last_advance is None:
            self._last = progress
            self._last_advance = now
        if progress != self._last:
            self._last = progress
            self._last_advance = now
            self._seen = True
        if self._seen:
            stalled = (now - self._last_advance) > self._stall
        else:
            stalled = (now - self._start) > self._startup_grace
        return not stalled


def _env_float(name: str, default: float, *, scale: float = 1.0) -> float:
    """Parse a positive float env var.  `scale` converts the env-var
    unit to the constant's unit (e.g. 1e-6 for µs→s) and is applied
    consistently to BOTH the env value and the default so the caller
    states `default` in the env-var's natural unit.  Invalid or
    non-positive values fall back to `default * scale` with a warning."""
    raw = os.environ.get(name)
    if raw is None:
        return default * scale
    try:
        v = float(raw) * scale
        if v <= 0:
            raise ValueError("must be > 0")
        return v
    except (ValueError, TypeError):
        logger.warning(
            "meteor-scatter: ignoring invalid %s=%r (using default %g)",
            name, raw, default,
        )
        return default * scale


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
        if v < 1:
            raise ValueError("must be >= 1")
        return v
    except (ValueError, TypeError):
        logger.warning(
            "meteor-scatter: ignoring invalid %s=%r (using default %d)",
            name, raw, default,
        )
        return default



def _sqlite_sink_available() -> bool:
    """True when the SQLite sink the hs-uploader reads from is in play.

    Mirrors `hs_uploader.sources.sqlite._ConnectionConfig.from_env`:
    an explicit `SIGMOND_SQLITE_PATH`, or the default sink file already
    on disk.  When the sink is in play the hs-uploader shim selects
    `SqliteSource`, so the per-slot `.spots.txt` spool files would
    never be consumed — the recorder skips writing them.
    """
    if (os.environ.get("SIGMOND_SQLITE_PATH") or "").strip():
        return True
    return Path("/var/lib/sigmond/sink.db").exists()


class MeteorScatterRecorder:
    """Orchestrates one or more ReceiverManagers from a single process.

    Accepts either a single ``radiod_block`` (legacy single-source
    deployments via ``--radiod-id``) or a list of blocks (multi-source
    deployments where the same process drives several radiods).
    """

    def __init__(
        self,
        config: dict,
        radiod_blocks: Union[dict, list[dict]],
        *,
        reporter_id: Optional[str] = None,
    ):
        self._config = config
        self._reporter_id = reporter_id
        # Accept dict (legacy single-source) or list (multi-source).
        # Internal storage is always a list for uniform iteration.
        if isinstance(radiod_blocks, dict):
            self._radiod_blocks: list[dict] = [radiod_blocks]
        else:
            self._radiod_blocks = list(radiod_blocks)
        if not self._radiod_blocks:
            raise ValueError(
                "MeteorScatterRecorder requires at least one [[radiod]] block"
            )
        # Convenience handles for code that pre-dated multi-source.
        # ``_radiod`` / ``_radiod_id`` refer to the FIRST block — only
        # safe for fields that are global across sources (e.g. station
        # info lives at config root, not the block).  Per-source state
        # — including the canonical ``rx_source`` tag — belongs in
        # the corresponding ReceiverManager.
        self._radiod = self._radiod_blocks[0]
        # Phase 6: canonical identifier is the mDNS status name.
        self._radiod_id = resolve_radiod_status(self._radiod)
        self._paths = config.get("paths", {})
        self._station = config.get("station", {})

        spool_root = Path(
            self._paths.get("spool_dir", "/var/lib/meteor-scatter"),
        )
        log_dir = Path(self._paths.get("log_dir", "/var/log/meteor-scatter"))

        # radiod LIFETIME tag (ka9q-python ≥3.13.0).  0 = no LIFETIME tag
        # sent + no keep-alive; >0 = self-destruct after N frames,
        # refreshed at frames/4 cadence while we're alive.  See DEFAULTS
        # in config.py.  Phase A of the WSPR fix proved the keepalive-
        # vs-expiry race wedges channels at Template defaults under
        # multi-source load; multi-source deployments should leave this at 0.
        proc = config.get("processing", {})
        self._radiod_lifetime_frames: int = int(
            proc.get("radiod_lifetime_frames", 0)
        )

        # One ReceiverManager per radiod_block; process-global state
        # (uploaders, lifetime thread, stats thread) lives below.
        self._receivers: list[ReceiverManager] = [
            ReceiverManager(
                config=config,
                radiod_block=block,
                spool_root=spool_root,
                log_dir=log_dir,
                radiod_lifetime_frames=self._radiod_lifetime_frames,
                reporter_id=self._reporter_id,
            )
            for block in self._radiod_blocks
        ]

        # Populated by _start_uploaders() with the HsPskReporterUploader
        # (unless METEOR_SCATTER_DELIVERY_MODE disables it).  _shutdown
        # iterates this to stop each uploader cleanly.
        self._uploaders: list = []
        self._running = False

        # Aggregate of every ReceiverManager's lifetime entries —
        # populated after ``provision_channels`` and refreshed by the
        # single process-global keepalive thread so we don't spawn
        # N threads for N sources.
        self._lifetime_entries: list[tuple[object, int]] = []
        self._lifetime_thread: Optional[threading.Thread] = None

        # Phase C: one MeteorScatterCycleBatcher per process; all
        # ReceiverManagers' ChTailers feed it.  Started in run()
        # before tailers spawn; stopped in _shutdown() after them.
        self._cycle_batcher: Optional[MeteorScatterCycleBatcher] = None

    # --- Per-source iteration helpers ---------------------------------

    @property
    def receivers(self) -> list[ReceiverManager]:
        """Read-only access to the per-source ReceiverManagers."""
        return list(self._receivers)

    def _iter_sinks(self):
        """Yield every ChannelSink across every ReceiverManager."""
        for rx in self._receivers:
            for sink in rx.sinks:
                yield sink

    # Settled-capture gate (V1 fix per
    # docs/TIMING-PIPELINE-WIRING.md §6.6 / §10.3).  Block on
    # ensure_channel() until chrony has reported a settled state
    # for SETTLE_REQUIRED_CYCLES consecutive readings, so the
    # per-channel ChannelInfo anchors captured by ka9q-python
    # inherit an ε_0 ≈ 0 system_time.  Without this gate, channels
    # whose SSRCs were created before chrony settled (or before a
    # radiod restart) carry stale anchors and produce slot
    # timestamps wrong by minutes to hours — corrupting psk.spots'
    # UTC field silently.  Verified 2026-05-11.
    #
    # Defaults assume bare-metal hosts with hardware GPS PPS where
    # chrony tracks within tens of µs.  On VMs and hosts with looser
    # discipline, chrony's Last offset may stably sit at 200-500 µs
    # — the 100 µs default would always time out.  Each constant
    # below is overridable via the matching `METEOR_SCATTER_SETTLE_*` env var:
    #
    #   METEOR_SCATTER_SETTLE_MAX_OFFSET_US     ceiling on |Last offset| (µs).
    #                                Set to e.g. 1000 on a VM.
    #   METEOR_SCATTER_SETTLE_REQUIRED_CYCLES   consecutive settled polls before
    #                                we consider chrony stable.
    #   METEOR_SCATTER_SETTLE_POLL_SEC          poll interval (s).
    #   METEOR_SCATTER_SETTLE_TIMEOUT_SEC       overall wait cap (s) before
    #                                proceeding with degraded anchors.
    #
    # All env reads happen at class-load time (process start), so a
    # restart picks up the new value.  Invalid values fall back to
    # the conservative default and log a warning at gate time.
    # Resolved at module-load time; env overrides apply per process.
    SETTLE_MAX_OFFSET_S = _env_float(
        "METEOR_SCATTER_SETTLE_MAX_OFFSET_US", 100.0, scale=1e-6,
    )
    SETTLE_REQUIRED_CYCLES = _env_int(
        "METEOR_SCATTER_SETTLE_REQUIRED_CYCLES", 3,
    )
    SETTLE_POLL_SEC = _env_float(
        "METEOR_SCATTER_SETTLE_POLL_SEC", 5.0,
    )
    SETTLE_TIMEOUT_SEC = _env_float(
        "METEOR_SCATTER_SETTLE_TIMEOUT_SEC", 60.0,
    )

    def run(self) -> None:
        """Main entry: provision channels, start streams, block until signal."""
        self._running = True
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        try:
            # V1 fix layer 1: gate ensure_channel() on chrony being
            # settled.  See docs/TIMING-PIPELINE-WIRING.md §6.6.
            # One gate, not per-source — when chrony is settled it's
            # settled for all radiods this process talks to.
            self._wait_for_chrony_settled()
            self._provision_all_receivers()
            self._start_all_streams()
            self._start_uploaders()
            self._start_all_ch_tailers()
            self._start_stats_thread()
            self._start_lifetime_keepalive()
            self._notify_ready()
            self._main_loop()
        except Exception:
            logger.exception("Fatal error in recorder")
        finally:
            self._shutdown()

    # --- Per-source orchestration -------------------------------------

    def _provision_all_receivers(self) -> None:
        """Drive each ReceiverManager's provision_channels.

        Decoder / spool config is process-global (one ka9q-radio binary,
        one decoder binary, one spool root), so resolve it once here and
        hand it to every manager.  Lifetime entries are gathered across
        managers for the single process-global keepalive thread.
        """
        decoder_kind = str(
            self._paths.get("decoder_kind", "jt9"),
        ).lower()
        # MSK144 decodes with WSJT-X's jt9 (`jt9 --msk144`).  An empty
        # decoder path means "resolve the bundled arch-specific binary at
        # runtime" (core.decoder._resolve_decoder_binary); an explicit
        # paths.decoder_jt9 / paths.decoder overrides that resolution.
        decoder = self._paths.get(
            "decoder_jt9", self._paths.get("decoder", ""),
        )
        keep_wav = self._paths.get("keep_wav", False)
        # Tee per-slot decoder output into <wav>.spots.txt files only
        # when there is no SQLite sink for the uploader's shim to read
        # — that's the file-fallback mode FileTreeSource picks up.
        spool_spots = not _sqlite_sink_available()
        logger.info(
            "decoder_kind=%s path=%s spool_spots=%s sources=%d",
            decoder_kind, decoder, spool_spots,
            len(self._receivers),
        )

        for rx in self._receivers:
            rx.provision_channels(
                decoder=decoder,
                decoder_kind=decoder_kind,
                keep_wav=keep_wav,
                spool_spots=spool_spots,
            )
            # Gather this manager's lifetime entries for the global
            # keepalive thread.  Each manager's list is stable after
            # provision_channels returns.
            self._lifetime_entries.extend(rx.lifetime_entries)

    def _start_all_streams(self) -> None:
        for rx in self._receivers:
            rx.start_streams()

    def _start_all_ch_tailers(self) -> None:
        callsign = self._station.get("callsign", "")
        grid = self._station.get("grid_square", "")
        try:
            from meteor_scatter.version import GIT_INFO
            short = (GIT_INFO or {}).get("short", "")
        except Exception:
            short = ""
        try:
            from importlib.metadata import version as pkg_version
            ver = pkg_version("meteor-scatter")
        except Exception:
            ver = "0.1.0"
        proc_version = f"{ver}+{short}" if short else ver

        # forward_to_pskreporter is a PSKReporter-era per-row flag with no
        # meaning for the wsprdaemon path; deposit-only mode always passes
        # False.  Spots land in the SQLite sink regardless — the flag only
        # ever gated PSKReporter forwarding, which this client does not do.
        forward_flag = False

        # One shared cycle batcher per process; every ReceiverManager's
        # tailers receive the same batcher reference, so spots collapse
        # into one batch per (cycle, source) before the SQLite write.
        if self._cycle_batcher is None:
            self._cycle_batcher = MeteorScatterCycleBatcher(
                writer_factory=_default_writer_factory,
            )
            self._cycle_batcher.start()

        for rx in self._receivers:
            rx.start_ch_tailers(
                callsign=callsign,
                host_grid=grid,
                proc_version=proc_version,
                forward_flag=forward_flag,
                cycle_batcher=self._cycle_batcher,
            )

    def _wait_for_chrony_settled(self) -> bool:
        """Block until chrony's Last offset has been below
        ``SETTLE_MAX_OFFSET_S`` for ``SETTLE_REQUIRED_CYCLES``
        consecutive readings.  Returns True if chrony settled within
        the timeout, False if we timed out (degraded mode, logged
        loudly).

        Capturing per-channel anchors when chrony is settled means
        the ChannelInfo's (gps_time, rtp_timesnap) pair inherits an
        ε_0 ≈ 0 system_time.  Sample-clock arithmetic in
        ka9q.rtp_to_utc then projects slot start times to
        true UTC ± ε_now (chrony's current discipline error), not
        ε_now − ε_0 with ε_0 frozen at the wrong value.

        Silent no-op when chronyc is unavailable.  See
        docs/TIMING-PIPELINE-WIRING.md §6.6 for the empirical
        evidence and §10.3 for the architectural pattern.
        """
        import subprocess as _sub
        try:
            _sub.run(['chronyc', '-h'], capture_output=True, timeout=2.0)
        except (FileNotFoundError, OSError, _sub.TimeoutExpired):
            logger.warning(
                "meteor-scatter settled-capture gate: chronyc unavailable — "
                "channel anchors will be captured without verification "
                "(ε_0 may be non-zero, V1 not prevented; "
                "slot timestamps may be silently wrong)"
            )
            return False

        consecutive = 0
        wait_start = time.monotonic()
        deadline = wait_start + self.SETTLE_TIMEOUT_SEC
        logger.info(
            "meteor-scatter settled-capture gate: waiting for chrony "
            "(threshold |Last offset| <= %.0f µs, need %d consecutive readings, "
            "timeout %.0fs)",
            self.SETTLE_MAX_OFFSET_S * 1e6,
            self.SETTLE_REQUIRED_CYCLES,
            self.SETTLE_TIMEOUT_SEC,
        )
        while time.monotonic() < deadline:
            try:
                proc = _sub.run(
                    ['chronyc', '-n', 'tracking'],
                    capture_output=True, text=True, timeout=5.0,
                )
            except (_sub.TimeoutExpired, OSError) as exc:
                logger.debug("meteor-scatter settled-capture: chronyc failed: %s", exc)
                time.sleep(self.SETTLE_POLL_SEC)
                consecutive = 0
                continue
            if proc.returncode != 0:
                time.sleep(self.SETTLE_POLL_SEC)
                consecutive = 0
                continue

            last_offset = self._parse_chronyc_last_offset(proc.stdout)
            if last_offset is None:
                logger.debug(
                    "meteor-scatter settled-capture: could not parse "
                    "Last offset from chronyc tracking output"
                )
                time.sleep(self.SETTLE_POLL_SEC)
                consecutive = 0
                continue

            if abs(last_offset) <= self.SETTLE_MAX_OFFSET_S:
                consecutive += 1
                logger.info(
                    "meteor-scatter settled-capture: chrony Last offset "
                    "%+.1f µs OK (%d/%d)",
                    last_offset * 1e6,
                    consecutive,
                    self.SETTLE_REQUIRED_CYCLES,
                )
                if consecutive >= self.SETTLE_REQUIRED_CYCLES:
                    elapsed = time.monotonic() - wait_start
                    logger.info(
                        "meteor-scatter settled-capture: chrony settled after "
                        "%.1fs — proceeding to provision channels", elapsed,
                    )
                    return True
            else:
                if consecutive > 0:
                    logger.info(
                        "meteor-scatter settled-capture: chrony Last offset "
                        "%+.1f µs > threshold; resetting counter",
                        last_offset * 1e6,
                    )
                consecutive = 0
            time.sleep(self.SETTLE_POLL_SEC)

        logger.warning(
            "meteor-scatter settled-capture: timeout after %.0fs — "
            "proceeding with degraded anchors (slot timestamps may "
            "be wrong on some channels; visible as future-dated "
            "WAV filenames per docs/TIMING-PIPELINE-WIRING.md §6.6)",
            self.SETTLE_TIMEOUT_SEC,
        )
        return False

    @staticmethod
    def _parse_chronyc_last_offset(text: str) -> Optional[float]:
        """Parse `chronyc tracking`'s ``Last offset`` line.

        Returns the offset in seconds (float), or None if unparseable.
        Matches the parser in hf-timestd's CoreRecorderV2.
        """
        for line in (text or '').splitlines():
            s = line.strip()
            if s.startswith('Last offset'):
                _, _, val = s.partition(':')
                val = val.strip()
                if not val:
                    return None
                token = val.split()[0]
                try:
                    return float(token)
                except ValueError:
                    return None
        return None

    def _start_uploaders(self) -> None:
        # MSK144 spots are attempted QSOs → published to PSKReporter the
        # same way FT4/FT8 are.  A single HsPskReporterUploader thread
        # pumps the ``psk.spots`` SQLite queue (filled by the ChTailer
        # → MeteorScatterCycleBatcher path) to pskreporter.info via the
        # hs-uploader PskReporterTcp transport (mode "msk144" → "MSK144").
        # The SqliteSource is selected when sigmond's sink is present;
        # else it falls back to the per-slot spool FileTreeSource.
        #
        # METEOR_SCATTER_DELIVERY_MODE controls this: "direct" (default)
        # runs the uploader; "deposit"/"off"/"none" leaves spots in the
        # sink only (record → decode → DB, no external publish).
        mode = (
            os.environ.get("METEOR_SCATTER_DELIVERY_MODE") or "direct"
        ).strip().lower()
        if mode in ("deposit", "off", "none", "disabled"):
            logger.info(
                "METEOR_SCATTER_DELIVERY_MODE=%s — PSKReporter uploader "
                "disabled; MSK144 spots deposit to the psk.spots sink only",
                mode,
            )
            return

        callsign = self._station.get("callsign", "")
        grid = self._station.get("grid_square", "")
        if not callsign or not grid:
            logger.warning(
                "callsign/grid not configured — PSKReporter uploader "
                "will not start",
            )
            return
        antenna = self._station.get("antenna", "")
        # Default to TCP (delivery-confirmed, no silent drops under load).
        use_tcp = bool(self._paths.get("pskreporter_tcp", True))
        spool_dir = Path(self._paths.get(
            "spool_dir", "/var/lib/meteor-scatter",
        )) / self._radiod_id

        uploader = HsPskReporterUploader(
            callsign=callsign,
            grid_square=grid,
            antenna=antenna,
            radiod_id=self._radiod_id,
            use_tcp=use_tcp,
            spool_dir=spool_dir,
        )
        logger.info(
            "uploader: %s (MSK144 → PSKReporter)", type(uploader).__name__,
        )
        uploader.start()
        self._uploaders.append(uploader)

    def _notify_ready(self) -> None:
        """Send sd_notify READY=1 if running under systemd."""
        try:
            addr = os.environ.get("NOTIFY_SOCKET")
            if addr:
                import socket
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    if addr.startswith("@"):
                        addr = "\0" + addr[1:]
                    sock.connect(addr)
                    sock.sendall(b"READY=1")
                finally:
                    sock.close()
                logger.info("sd_notify READY=1 sent")
        except Exception:
            logger.debug("sd_notify failed (not running under systemd?)")

    def _start_stats_thread(self) -> None:
        self._stats_thread = threading.Thread(
            target=lambda: _supervise(
                "stats", lambda: self._running, self._stats_loop,
            ),
            daemon=True, name="stats",
        )
        self._stats_thread.start()

    def _start_lifetime_keepalive(self) -> None:
        """Refresh radiod's LIFETIME on every active SSRC at frames/4 cadence.

        No-op when radiod_lifetime_frames is 0 or no channels opted in.
        Failure to refresh (network blip, radiod restart) must not crash
        the recorder — log and continue; MultiStream's drop/restore path
        will re-apply the slot's lifetime when reception resumes.
        """
        if not self._lifetime_entries:
            return
        # Refresh every quarter of the lifetime — gives 4× safety margin
        # against radiod self-destruct if a single refresh is missed.
        # Floor at 1 s so absurd configs don't busy-loop.
        interval = max(self._radiod_lifetime_frames / 50.0 / 4.0, 1.0)
        logger.info(
            "lifetime keepalive: %d channels, %d frames, refresh every %.1fs",
            len(self._lifetime_entries),
            self._radiod_lifetime_frames,
            interval,
        )
        self._lifetime_thread = threading.Thread(
            target=lambda: _supervise(
                "lifetime", lambda: self._running,
                self._lifetime_loop, interval,
            ),
            daemon=True,
            name="lifetime",
        )
        self._lifetime_thread.start()

    def _lifetime_loop(self, interval_sec: float) -> None:
        while self._running:
            time.sleep(interval_sec)
            if not self._running:
                break
            for multi, ssrc in self._lifetime_entries:
                try:
                    multi.set_channel_lifetime(
                        ssrc, self._radiod_lifetime_frames
                    )
                except Exception as exc:
                    logger.warning(
                        "lifetime keepalive failed (ssrc=%s): %s", ssrc, exc,
                    )

    def _stats_loop(self) -> None:
        """Every 60 s, log a summary of decode + spot activity per mode.

        Spot count comes from counting lines added to each mode-log file
        (the normalized jt9 lines slot.py writes and the ChTailer reads).
        Decode count comes from each SlotWorker's own counters.
        """
        log_dir = Path(self._paths.get("log_dir", "/var/log/meteor-scatter"))
        prev_ok: dict[str, int] = {}
        prev_fail: dict[str, int] = {}
        prev_empty: dict[str, int] = {}
        prev_spot_lines: dict[str, int] = {}

        def count_lines(p: Path) -> int:
            try:
                with open(p, "rb") as f:
                    return sum(1 for _ in f)
            except OSError:
                return 0

        # Align first report to the minute boundary + 60 s so the first
        # window isn't a partial-minute artifact.
        time.sleep(60.0)

        # Per-(radiod, mode) line-count tracking — keys "<rid>:<mode>".
        # Spot-log file is per-radiod so multi-source aggregation must
        # sum the deltas, not point at one file.

        while self._running:
            # Aggregate per (radiod, mode) so the multi-source case
            # surfaces each source's contribution.  Single-source
            # deployments emit one line per mode exactly like before.
            by_key: dict[tuple[str, str], dict] = {}
            for sink in self._iter_sinks():
                snap = sink.stats_snapshot()
                m = snap["mode"]
                rid = getattr(sink, "radiod_id", "") or ""
                # Some sinks predate the radiod_id attribute; fall
                # back to the rx that owns them.
                if not rid:
                    for rx in self._receivers:
                        if sink in rx.sinks:
                            rid = rx.radiod_id
                            break
                key = (rid, m)
                agg = by_key.setdefault(key, {
                    "freqs": 0, "decodes_ok": 0, "decodes_fail": 0,
                    "slots_empty": 0,
                })
                agg["freqs"] += 1
                agg["decodes_ok"] += snap["decodes_ok"]
                agg["decodes_fail"] += snap["decodes_fail"]
                agg["slots_empty"] += snap["slots_empty"]

            for (rid, mode), agg in by_key.items():
                spot_log = log_dir / f"{rid}-{mode}.log"
                spot_lines_total = count_lines(spot_log)
                prev_key = f"{rid}:{mode}"
                spots_delta = spot_lines_total - prev_spot_lines.get(
                    prev_key, spot_lines_total,
                )
                ok_delta = agg["decodes_ok"] - prev_ok.get(prev_key, 0)
                fail_delta = agg["decodes_fail"] - prev_fail.get(prev_key, 0)
                empty_delta = agg["slots_empty"] - prev_empty.get(prev_key, 0)

                # Include the radiod_id tag so multi-source operators
                # can tell which source is producing what; single-
                # source readers can still grep ``stats FT8`` etc.
                logger.info(
                    "stats %s rx=%s: spots=%d decodes=%d/%d "
                    "slots_empty=%d freqs=%d (60s window)",
                    mode.upper(), rid, spots_delta,
                    ok_delta, ok_delta + fail_delta,
                    empty_delta, agg["freqs"],
                )

                prev_ok[prev_key] = agg["decodes_ok"]
                prev_fail[prev_key] = agg["decodes_fail"]
                prev_empty[prev_key] = agg["slots_empty"]
                prev_spot_lines[prev_key] = spot_lines_total

            time.sleep(60.0)

    def _pipeline_progress(self):
        """Monotonic count of decode slots processed across all channels:
        sum of each SlotWorker's (decodes_ok + decodes_fail + slots_empty).

        Advances every decode cadence (<=15 s) whenever RTP is flowing --
        and, even if every decode hangs, at least every ~60 s via the
        kill-deadline reap (see slot.DECODE_TIMEOUT_SEC) -- regardless of
        whether anything actually decodes.  Freezes only when the
        record->decode pipeline has wedged.  Returns None on any error so the
        watchdog fails safe (keeps pinging).
        """
        try:
            total = 0
            for sink in self._iter_sinks():
                snap = sink.stats_snapshot()
                total += (snap.get("decodes_ok", 0)
                          + snap.get("decodes_fail", 0)
                          + snap.get("slots_empty", 0))
            return total
        except Exception:
            return None

    def _main_loop(self) -> None:
        """Block until signalled, petting the systemd watchdog only while the
        record->decode pipeline is making progress.

        Pinging unconditionally would keep a wedged (not crashed) daemon alive
        forever.  _ProgressGate withholds WATCHDOG=1 once the pipeline has
        stalled past the threshold, so systemd's WatchdogSec restarts it;
        a healthy-but-quiet recorder (no signal to decode) still advances the
        slot counters and keeps pinging.
        """
        watchdog_usec = os.environ.get("WATCHDOG_USEC")
        pet_interval = (
            int(watchdog_usec) / 1_000_000 / 2
            if watchdog_usec else 30.0
        )
        # stall_sec < WatchdogSec(120) and comfortably > the worst-case
        # ~60 s slot-progress interval and any radiod re-provision window.
        gate = _ProgressGate(stall_sec=90.0)

        while self._running:
            time.sleep(min(pet_interval, 5.0))
            if gate.update(self._pipeline_progress(), time.monotonic()):
                self._pet_watchdog()
            else:
                logger.error(
                    "record->decode pipeline stalled; withholding systemd "
                    "watchdog ping so the unit is restarted",
                )

    def _pet_watchdog(self) -> None:
        try:
            addr = os.environ.get("NOTIFY_SOCKET")
            if addr:
                import socket
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    if addr.startswith("@"):
                        addr = "\0" + addr[1:]
                    sock.connect(addr)
                    sock.sendall(b"WATCHDOG=1")
                finally:
                    sock.close()
        except Exception:
            pass

    def _on_signal(self, signum, frame) -> None:
        logger.info("Received signal %d, shutting down", signum)
        self._running = False

    def _shutdown(self) -> None:
        logger.info("Shutting down...")
        for uploader in self._uploaders:
            try:
                uploader.stop()
            except Exception:
                logger.exception("Error stopping uploader")
        # Stop each ReceiverManager — handles its own ChTailers,
        # MultiStreams, sinks, log fds, and RadiodControl close.
        # Stopping tailers first ensures no new rows hit the batcher
        # while it's draining.
        for rx in self._receivers:
            try:
                rx.stop()
            except Exception:
                logger.exception(
                    "Error stopping ReceiverManager %s", rx.radiod_id,
                )
        # Drain + stop the cycle batcher last so any spots already
        # queued in its pending batches make it to psk.spots before
        # the process exits.
        if self._cycle_batcher is not None:
            try:
                self._cycle_batcher.stop()
            except Exception:
                logger.exception("Error stopping cycle batcher")
        logger.info("Shutdown complete")


# ``_resolve_encoding`` is re-exported from ``receiver_manager`` at
# the top of this module so existing ``from meteor_scatter.core.recorder
# import _resolve_encoding`` lines keep working.  No new definition
# here — single source of truth lives in receiver_manager.py.
