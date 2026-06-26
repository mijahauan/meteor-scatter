# Architecture

For contributors. The high-level pipeline:

```
radiod (ka9q-radio)
  │  RadiodControl.ensure_channel() via ka9q-python
  │  preset=usb, samprate=12000, encoding=s16be
  ▼
RTP multicast
  │  one MultiStream per radiod (shared socket, demux on SSRC)
  ▼
meteor-scatter daemon (one per radiod)
  │
  ├── ChannelSink (per channel)
  │     ├─ Ring buffer (collections.deque + Lock)
  │     └─ SlotWorker
  │          ├─ extract slot at FT8/FT4 cadence boundary
  │          ├─ wav.write_wav(...) → spool
  │          └─ subprocess: decode_ft8 [-4] -f <mhz> <wav>
  │                          │ stdout appended to spot log
  │
  └── PskReporterUploader (per mode)
        └─ subprocess: pskreporter-sender [--tcp] <spotlog> <mode>
              (long-running, tails the spot log)
```

One `meteor-scatter@<radiod_id>.service` per radiod. One uploader
subprocess per mode (so two: FT8 and FT4). N decoder forks per minute,
where N = (channels × slots-per-min).

## Source layout

```
src/meteor_scatter/
  cli.py              # argparse, log-level resolution, stdout-cleanliness guard
  config.py           # TOML loader, defaults, radiod block resolution
  contract.py         # inventory/validate JSON builders (sigmond contract v0.8)
  version.py          # GIT_INFO dict (sha, ref, dirty)
  daemon.py           # daemon entrypoint: load config, build MeteorScatterRecorder, run
  core/
    recorder.py       # MeteorScatterRecorder: orchestrates one radiod's channels
    stream.py         # ChannelSink: ring + slot worker driven by RTP callbacks
    ring.py           # Ring: process-local audio + timestamp deque
    slot.py           # SlotWorker: cadence math, WAV write, fork decoder
    wav.py            # write_wav(): 16-bit PCM RIFF writer
    hs_uploader_shim.py  # HsPskReporterUploader: hs-uploader Pipeline upload path
    ch_tailer.py      # ChTailer: tails spot logs → psk.spots via hamsci_sink.Writer
```

The PSK Reporter upload path is `HsPskReporterUploader`: it ships rows
via the `hs-uploader` library's `Pipeline` + `PskReporterTcp` transport,
sourcing from sigmond's local SQLite sink (`SqliteSource`) or, when no
sink is present, the per-slot `.spots.txt` spool (`FileTreeSource`).

`ChTailer` is the producer for the SQLite sink: one thread per
`(radiod, mode)` tails the same spot log, parses each line, and inserts
into `psk.spots` via `sigmond.hamsci_sink.Writer.from_env()` — sigmond's
local SQLite sink (`/var/lib/sigmond/sink.db`) by default. It resolves
to a no-op when the sink path is unwritable, so meteor-scatter still runs
standalone with no sigmond present.

### Delivery mode (`METEOR_SCATTER_DELIVERY_MODE`)

Three operator-selectable routes for how a spot reaches PSKReporter:

| `METEOR_SCATTER_DELIVERY_MODE` | Direct uploader | ChTailer (→ local sink → wsprdaemon tar) | Row tag `forward_to_pskreporter` |
|---|---|---|---|
| `server` (default) | off          | on   | true (server posts on our behalf) |
| `direct`           | on           | off  | n/a (no row written) |
| `both`             | on           | on   | false (avoid double-post) |

The `server` default centralizes PSKReporter delivery responsibility on
the wsprdaemon servers' gw1-elected `pskreporter_forwarder` — clients
that pick it save the bandwidth of a second outbound POST per cycle,
and the server retries on the client's behalf during PSKReporter
outages. Choose `direct` only when you don't want the wsprdaemon
servers to handle delivery; choose `both` when you want belt-and-suspenders
redundancy (you POST directly + the server has a copy but won't re-post).

Unknown / empty values fall back to `server` with a one-shot warning,
so an operator typo doesn't silently disable delivery.

## Per-module responsibilities

### `core/recorder.py` — `MeteorScatterRecorder`

Owns one radiod's connection. Calls `RadiodControl.ensure_channel(...)`
for each `(freq, mode)` configured, captures the resolved
`ChannelInfo` (multicast destination, SSRC), creates a `ChannelSink`
per channel, and starts a `MultiStream` per radiod that demuxes RTP
into the per-channel sinks. Runs the systemd `sd_notify` watchdog
loop, the per-minute stats logger, and signal handlers (SIGTERM,
SIGINT, SIGHUP).

### `core/stream.py` — `ChannelSink`

Passive callback target. Holds one `Ring` and one `SlotWorker`. RTP
callbacks (`on_samples`, `on_stream_dropped`, `on_stream_restored`)
push into the ring or signal gaps. Owns no thread or socket.

### `core/ring.py` — `Ring`

Process-local audio buffer: a `deque` of `(samples_ndarray,
utc_start_seconds)` pairs guarded by a `threading.Lock`. Supports
`extract_slot(slot_start, duration)` returning a contiguous
`np.float32` array sliced to frame-accurate boundaries. Old data ages
out automatically as new pushes exceed buffer capacity.

### `core/slot.py` — `SlotWorker`

The cadence engine. Wakes on a timer aligned to FT8 (15 s) or FT4
(7.5 s) UTC slot boundaries, asks the ring for that slot's audio,
writes a WAV, forks `decode_ft8` with the right flags, deletes the WAV
when the decoder exits (unless `keep_wav = true`), and increments
counters that `recorder.py` reads for the per-minute stats line.

### `core/wav.py` — `write_wav()`

Minimal 16-bit signed PCM RIFF writer. Takes a `np.float32` array,
clamps to `[-1, 1]`, scales to int16, writes the standard 44-byte
header. No external dependency on `scipy`/`soundfile`.

### `core/hs_uploader_shim.py` — `HsPskReporterUploader`

The PSK Reporter upload path. Runs a daemon thread that pumps
`psk.spots` rows through the `hs-uploader` library's `Pipeline` +
`PskReporterTcp` transport, sourcing from sigmond's local SQLite sink
(`SqliteSource`) or, when no sink is present, the per-slot
`.spots.txt` spool (`FileTreeSource`).

### `cli.py` and `daemon.py`

`cli.py` is the only entrypoint (`meteor-scatter = meteor_scatter.cli:main`).
It dispatches to `inventory`, `validate`, `version`, `daemon`, or
`status`. The first three keep stdout pristine for JSON consumers; the
last two log normally.

`daemon.py` is the loop entered by `cli daemon`: load config, resolve
the `[[radiod]]` block, build `MeteorScatterRecorder`, run until signal.

## Key design decisions

- **One unit instance per radiod**, not per host. Templated unit
  `meteor-scatter@<radiod_id>.service`, parameterized by `--radiod-id`.
- **ka9q-python owns the multicast destination.** meteor-scatter never
  passes `destination=` to `ensure_channel()`; it reads the resolved
  address from the returned `ChannelInfo` for the inventory output.
  Required by [SIGMOND-CONTRACT.md §7](SIGMOND-CONTRACT.md).
- **radiod identified by mDNS hostname**, never IP. mDNS is the
  source of truth for radiod presence on the LAN.
- **Process-local ring buffer.** No SysV IPC, no shared memory — the
  recorder is the only consumer.
- **Subprocess decoder and uploader.** meteor-scatter shells out to
  `decode_ft8` and `pskreporter-sender` rather than reimplementing
  either. This keeps the Python side small and makes upgrades
  independent.
- **WAV spool is ephemeral.** Default `keep_wav = false` deletes
  WAVs after the decoder exits. ~10 MB/min/channel adds up fast.
- **MultiStream-based demux.** All channels for a radiod share one
  socket; ka9q-python demuxes by SSRC. Cheaper than one socket per
  channel.

## How a spot becomes a report

1. `radiod` emits RTP packets for one channel into its multicast
   group, with a per-channel SSRC.
2. The radiod's `MultiStream` receives the packet, demuxes by SSRC,
   and calls `ChannelSink.on_samples(samples, rtp_ts)`.
3. `Ring.push(...)` appends to the deque.
4. `SlotWorker._tick()` fires at the next 15 s (or 7.5 s) UTC
   boundary, calls `Ring.extract_slot(...)`, gets a clean
   180_000-sample (or 90_000-sample) `np.float32` array.
5. `wav.write_wav(...)` writes
   `/var/lib/meteor-scatter/<id>/<mode>/YYMMDD_HHMMSS_<freqkhz>.wav`.
6. `subprocess.Popen(["/usr/local/bin/decode_ft8", "-f", "14.074000",
   wav])` forks. The decoder writes spot lines to stdout, which the
   slot worker captures and appends to
   `/var/log/meteor-scatter/<id>-ft8.log`.
7. The long-running `pskreporter-sender` (started at recorder
   startup) is `tail -f`-ing that log. New lines accumulate in its
   in-memory spot list.
8. Every ~3 min (server-recommended interval, plus jitter), the
   sender uploads a batch to `report.pskreporter.info:4739` — UDP by
   default, TCP if `pskreporter_tcp = true`.
9. The recorder deletes the WAV (unless `keep_wav = true`).

## Testing

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

Tests focus on the contract surface (CLI stdout cleanliness, JSON
shape, config loading) rather than the audio pipeline (which needs a
real radiod). See [tests/test_contract.py](../tests/test_contract.py).
