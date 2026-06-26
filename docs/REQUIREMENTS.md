# meteor-scatter — Requirements Specification

**Status:** v0.1 baseline (retroactive). **Owner:** Michael Hauan (AC0G).
**Last reconciled against code:** meteor-scatter `0.1.0` (pyproject) / `0.4.0`
(deploy.toml) / contract `0.8` (code), git `cfc0bc2` (2026-06-25).
**Prefix:** `MTS`.

> Application of [sigmond/docs/REQUIREMENTS-TEMPLATE.md](https://github.com/HamSCI/sigmond/blob/main/docs/REQUIREMENTS-TEMPLATE.md)
> at the **Active** end of the maturity range (contract ~0.7 — full
> self-describe surface, live on `sigma`, but the science/upload tail is
> still being wired). The sigmond↔component **interface** requirements are
> specified once in the [client contract](https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md)
> and referenced — not restated — here (§8.3). Provenance tags:
> `[DOC]` documented · `[CODE]` implicit-in-code · `[NEW]` surfaced by this review.
> Status: ✅ implemented · 🟡 partial/unverified · ⬜ planned.
>
> **Reconciliation note (load-bearing).** This is the most drift-heavy doc in
> the suite. The component was scaffolded from the **psk-recorder** skeleton
> and renamed `msk144-recorder` → `meteor-scatter`; the *implemented reality*
> is **MSK144 / `jt9 --msk144` → `msk144.spots`**, but a substantial residue
> of the FT4/FT8 / `decode_ft8` / PSKReporter-shell parent still lives in
> `README.md`, `deploy.toml [[deps.git]]` + `[client_features]`, and
> `docs/SIGMOND-CONTRACT.md`. Where a requirement reconciles against
> contradictory sources, the **code** (`contract.py`, `core/decoder.py`,
> `core/ch_tailer.py`, `core/recorder.py`, the per-instance config, and
> `inventory --json`) is treated as ground truth and the drift is recorded
> as a `[NEW]` gap (§12).

## 1. Context & problem statement

Meteor trails ionize for a few milliseconds to ~2 seconds ("pings"), opening
ultra-short HF/VHF propagation windows. **MSK144** (WSJT-X's FEC'd successor to
FSK441) is engineered for exactly that regime — short 72 ms frames repeated
through a 15 s T/R period with aggressive LDPC FEC, so a single ping can carry a
full decode. meteor-scatter is the suite's **meteor-scatter ping
recorder/decoder**: it receives the conventional MSK144 monitoring channels
(10 m @ 28.130 MHz, 6 m @ 50.260 MHz) from `radiod` via `ka9q-python`, records
15 s T/R-aligned WAV slots, decodes each once with the bundled `jt9 --msk144`
binary, and ships the resulting spots to the suite's local SQLite sink
(`msk144.spots`) and onward to a reporting service.

This is a **monitoring / reporting** use, not a real-time QSO: `jt9` in MSK144
mode finds *all* pings in a slot and each is reported as a spot, so the slotted
recorder architecture (shared with psk/wspr) applies almost directly without
WSJT-X's within-period re-decode loop. The component is a well-behaved sigmond
client — templated per-radiod systemd unit, the full self-describe contract
surface, compound-callsign hash resolution via the shared `callhash` library so
a call learned on FT8/WSPR resolves an MSK144 hash and vice-versa — and runs
standalone (radiod + this component) with no sigmond present.

Its design lineage is deliberate: **psk-recorder's slotted skeleton + wspr's
`jt9` decode and arch-resolution**. That heritage is the source of both its
maturity (the 2026-06-12 resilience-sweep hardening came for free) and its
documentation drift (§12).

## 2. Goals & objectives

- **Detect and report MSK144 meteor-scatter pings** heard on the 10 m and 6 m
  monitoring channels, one spot per decoded ping.
- Decode with the **suite-standard `jt9` binary** (`jt9 --msk144`), not a
  re-implemented decoder — reuse, not reinvention.
- Recover **compound callsigns** from `jt9 -Y` hashes via the shared
  `callhash` table, cross-pollinating with FT8/WSPR sightings.
- Emit spots to the **local SQLite sink** (`msk144.spots`) as the durable
  artefact, and onward to a reporting service (PSKReporter direct, per the
  current code path) with cross-rx dedup available.
- Run as a **well-behaved suite client** (templated per-radiod unit, full
  contract self-describe, `Type=notify` + watchdog, off radiod cores) *and*
  fully **standalone**.
- Be **legible under drift** — `inventory --json` names the exact config it
  loaded, the sink target, and the timing-authority state.

## 3. Non-goals / out of scope

- **Re-implementing the MSK144 decoder.** It shells out to `jt9`; the decoder
  is an external WSJT-X dependency (Owner: WSJT-X / ka9q-bundled binary).
- **Re-implementing the upload protocol.** Upload is delegated to the
  `hs-uploader` library's `PskReporterTcp` transport (or a downstream
  forwarder); meteor-scatter does not speak the PSKReporter/wsprdaemon wire
  protocol itself.
- **Tuning hardware / selecting multicast.** It consumes pre-provisioned RTP
  from `radiod`; multicast destinations are *resolved from* radiod via
  `ChannelInfo`, never specified (Owner: ka9q-radio / ka9q-python).
- **Producing a timing authority.** It is a §18 *consumer* (currently
  RTP-default, authority read-but-not-applied); the authority producer is
  hf-timestd.
- **Sub-second timestamping.** MSK144 spot times are slot-quantized to the
  15 s T/R boundary; chain-delay correction is a contract hook, not applied
  (FT/MSK timestamps are ~1 s accurate, outside the chain-delay regime).
- **Real-time QSO / TX.** Strictly one-way passive monitoring.

## 4. Stakeholders & actors

Station operator · `radiod` (RTP audio source, required) · `ka9q-python`
(`RadiodControl.ensure_channel`, channel provisioning + multicast resolution) ·
the bundled **`jt9`** decoder (external WSJT-X binary, required) · the shared
`callhash` library (compound-call hash resolution) · `sigmond.hamsci_sink`
(local SQLite sink `msk144.spots`) · `hs-uploader` (upload transport +
watermark/retry state) · PSKReporter.info / wsprdaemon.org (upstream reporting
target) · `hf-timestd` (§18 timing-authority producer, optional) · sigmond
(multi-instance lifecycle, CPU affinity, coordination.env, status enrichment).

## 5. Assumptions & constraints

- `MTS-C-001` `[DOC]` ✅ `radiod` (ka9q-radio) SHALL be present and multicasting
  the configured MSK144 channels; meteor-scatter talks to it exclusively via
  `ka9q-python`, never the radiod control protocol directly.
- `MTS-C-002` `[CODE]` ✅ The `jt9` MSK144 decoder SHALL be available — bundled
  arch-resolved in-repo at `bin/decoders/jt9-{x86,arm32,arm64}-v*` (mirroring
  wspr-recorder), with an explicit `paths.decoder_jt9` override and a bare
  `jt9`-on-PATH fallback. **External dependency: WSJT-X / the jt9 binary.**
- `MTS-C-003` `[CODE]` ✅ One systemd instance SHALL run **per radiod**
  (`meteor-scatter@<id>.service`); each instance handles all configured MSK144
  frequencies on that radiod. The instance name `%i` is both `--instance` (spool
  key) and `--radiod-id` (legacy block selector).
- `MTS-C-004` `[CODE]` ✅ Python ≥3.10; siblings `ka9q-python`, `callhash`,
  `hs-uploader` SHALL be editable installs (`[tool.uv.sources]`) so a `git pull`
  propagates without reinstall.
- `MTS-C-005` `[DOC]` ✅ The `usb` preset filter (~300–2950 Hz passband) SHALL NOT
  be narrowed: MSK144 occupies ~2.4–2.5 kHz centred on 1500 Hz audio and a
  tighter filter would clip the sidebands and kill decodes.
- `MTS-C-006` `[CODE]` ✅ The RTP sample stream SHALL be the slot-timing
  substrate; the slot UTC stamped on each decode comes from the RTP-anchored slot
  boundary, never jt9's own (WAV-filename-derived) time column.
- `MTS-C-007` `[NEW]` 🟡 PSWS station/instrument IDs SHALL be optional;
  meteor-scatter operates without them (fields exist only for operators who also
  run PSWS).

## 6. Functional requirements

### 6.1 Acquisition & slotting
- `MTS-F-001` `[CODE]` ✅ SHALL provision each configured MSK144 channel via
  `ka9q-python` `RadiodControl.ensure_channel` (preset `usb`, samprate 12000,
  encoding `s16be`) **without** passing `destination=`, reading the resolved
  multicast address from `ChannelInfo` (contract §7).
- `MTS-F-002` `[CODE]` ✅ SHALL maintain a process-local ring buffer per channel
  (`collections.deque` behind a lock — no SysV IPC) feeding a per-channel
  `SlotWorker`.
- `MTS-F-003` `[DOC]` ✅ SHALL align slots to the **15 s MSK144 T/R cadence**
  (`MSK144_TR_PERIOD_SEC = 15`) and write one mono `s16be` 12 kHz WAV per slot.
- `MTS-F-004` `[CODE]` ✅ SHALL delete the slot WAV after decode by default
  (`paths.keep_wav = false`).

### 6.2 Decode (external jt9)
- `MTS-F-010` `[CODE]` ✅ SHALL fork `jt9 -Y --msk144 -p 15 -f 1500 -a <workdir>
  <wav>` per slot (cwd = workdir, pre-touching `plotspec`/`decdata` sentinels),
  bounded by the resilience-sweep decode-timeout pattern.
- `MTS-F-011` `[CODE]` ✅ SHALL read decodes from the **delta appended to
  `decoded.txt`** in the jt9 data dir (not stdout, which carries only the
  `<DecodeFinished>` sentinel), capping the file at `MAX_DECODED_TXT_BYTES`.
- `MTS-F-012` `[CODE]` ✅ SHALL normalize each decode to the per-mode log line
  `YYYY/MM/DD HH:MM:SS <snr_db> <dt> <abs_freq_hz> & <message>`, where
  `abs_freq_hz` = channel dial + jt9 audio offset and `&` is the MSK144 sync
  marker; the UTC comes from the slot anchor (`MTS-C-006`), not jt9.
- `MTS-F-013` `[CODE]` 🟡 The exact `decoded.txt` column layout SHALL be validated
  against a **real** MSK144 decode; the parser is deliberately tolerant
  (HHMM/HHMMSS, optional sync token) pending live confirmation. *(gap —
  `MTS-F-090`.)*
- `MTS-F-014` `[DOC]` ✅ SHALL invoke jt9 with `-Y` so unresolved compound calls
  emit as numeric `<NNNNNNN>` hashes for `callhash` resolution.

### 6.3 Spot construction & callhash resolution
- `MTS-F-020` `[CODE]` ✅ `ch_tailer` SHALL tail `<log_dir>/<radiod_id>-msk144.log`,
  parse each new line via the shared `callhash.parse_message`, and build a spot
  row (`mode=msk144`, `decoder_kind=jt9`, `snr_db`, `dt`, absolute `frequency`,
  resolved `tx_call`/`grid`/`message`).
- `MTS-F-021` `[DOC]` ✅ SHALL maintain a **per-radiod, cross-mode** `CallHashTable`
  (`observe()` + `parse_message(..., table=...)`), persisted to disk so a call
  learned on FT8/WSPR resolves an MSK144 hash and vice-versa, refusing to guess
  on ambiguous/colliding slots.
- `MTS-F-022` `[CODE]` ✅ Each row SHALL carry both `instance` (= radiod_id, legacy,
  removed in sigmond Phase 9) and `reporter_id` (per-instance `[instance]` value
  or radiod_id fallback), `rx_source` (`radiod:<status>`), and a
  `frequency_bucket_hz` (100 Hz bucket) for cross-rx dedup.
- `MTS-F-023` `[CODE]` ✅ Each row SHALL carry a `timing_authority` provenance block
  read from hf-timestd's §18 authority, degrading to the standalone-fallback
  marker when absent/stale (`authority_reader.py`).

### 6.4 Sink output
- `MTS-F-030` `[CODE]` ✅ SHALL stage rows into the local SQLite sink target
  **`msk144.spots`** via `sigmond.hamsci_sink.Writer.from_env(table="spots",
  mode="msk144", schema_version=2)`, resolving to a clean no-op only when the
  sink path is unwritable (standalone-safe).
- `MTS-F-031` `[CODE]` ✅ Rows MAY flow through a `MeteorScatterCycleBatcher`
  (cycle-aligned commit, single writer thread owning the thread-bound SQLite
  connection) when present; otherwise the tailer owns its own writer (legacy
  single-tailer path).
- `MTS-F-032` `[CODE]` 🟡 When no SQLite sink is writable, SHALL fall back to
  per-slot `.spots.txt` spool files (`FileTreeSource`, delete-on-ack) for the
  uploader.

### 6.5 Upload / delivery
- `MTS-F-040` `[CODE]` 🟡 SHALL pump the `msk144.spots` queue to **PSKReporter**
  via the in-process `HsPskReporterUploader` (`hs-uploader` `Pipeline` +
  `PskReporterTcp`, mode `msk144`→`MSK144`), at a 30 s pump cadence, with
  watermark/retry state in `/var/lib/hs-uploader/watermarks.db`. *(See drift
  note `MTS-F-091`: upload target is contradictorily documented.)*
- `MTS-F-041` `[CODE]` ✅ `METEOR_SCATTER_DELIVERY_MODE` SHALL select delivery:
  `direct` (default — run the uploader) vs `deposit`/`off`/`none` (sink-only).
  The uploader SHALL refuse to start without callsign/grid configured.
- `MTS-F-042` `[CODE]` 🟡 Cross-rx dedup (`METEOR_SCATTER_DIRECT_DEDUP`, opt-in,
  default OFF) SHALL collapse duplicate spots across receivers via a
  window-function CTE keyed on `(time, tx_call, frequency_bucket_hz)`. Default
  OFF because the CTE trips `disk I/O error` when sharing `sink.db` with
  wspr-recorder. *(gap.)*

### 6.6 Self-description & config (contract surface)
- `MTS-F-050` `[CODE]` ✅ SHALL implement `inventory --json` / `validate --json` /
  `version --json` per contract (declares `CONTRACT_VERSION = "0.8"`), with a
  stdout-cleanliness guard redirecting logging to stderr.
- `MTS-F-051` `[CODE]` ✅ `validate` SHALL **fail** on: no `[[radiod]]` blocks; a
  block missing `status`; the unconfigured placeholder status; an SSRC collision
  on `(freq, preset, sample_rate, encoding)` within a block. SHALL **warn** on:
  empty callsign/grid; missing MSK144 freqs; an unresolvable decoder override.
- `MTS-F-052` `[CODE]` ✅ SHALL implement `config init|edit|show|apply` and
  `env show|apply` per contract §13/§14 — a whiptail wizard (stdin fallback)
  honoring the §14.3 env bag (`STATION_*`, `SIGMOND_INSTANCE`,
  `SIGMOND_RADIOD_STATUS`), with `--json` entry points for sigmond tooling.
- `MTS-F-053` `[CODE]` ✅ `config.resolve_config_path()` SHALL prefer
  `/etc/meteor-scatter/<instance>.toml` when present, else fall back to the
  legacy shared `meteor-scatter-config.toml` with a one-line deprecation warning.
- `MTS-F-054` `[CODE]` ✅ SHALL read `RADIOD_<id>_CHAIN_DELAY_NS` from env on
  startup/SIGHUP and surface it as `chain_delay_ns_applied` (hook only — not
  applied to sample→UTC conversion; `[timing].chain_delay_ns` is the standalone
  fallback).

### 6.7 Resilience (resilience-sweep parity)
- `MTS-F-060` `[CODE]` ✅ SHALL run as `Type=notify` with `WatchdogSec=120`,
  `Restart=always`, and a placeholder fail-fast (`exit 78` +
  `RestartPreventExitStatus=78`) so an unconfigured radiod stops cleanly rather
  than crash-looping.
- `MTS-F-061` `[NEW]` 🟡 The watchdog heartbeat SHALL be tied to a
  **signal-independent** counter (RTP samples received), NOT decode output —
  meteor pings are rare, so liveness must not depend on spots. *(Design intent
  from METEOR-SCATTER-DESIGN.md §3; presence of the progress-gate watchdog in
  this build is unverified by this review — confirm.)*

## 7. Quality / non-functional requirements

- `MTS-Q-001` `[CODE]` ✅ Decode SHALL be the only subprocess; the uploader is
  in-process (the legacy `pskreporter`/`decode_ft8` subprocesses were removed).
- `MTS-Q-002` `[CODE]` ✅ Sink writes SHALL degrade to a graceful no-op when the
  shared DB is unavailable; the per-mode log file remains the local artefact.
- `MTS-Q-003` `[CODE]` ✅ The unit SHALL keep `/var/lib/hs-uploader` group-shared
  (root:sigmond 02775 via tmpfiles.d) and SHALL NOT re-chown it — a prior
  re-stomp locked wsprdaemon-client out of the shared `watermarks.db`.
- `MTS-Q-004` `[CODE]` ✅ The unit SHALL constrain memory (`MemoryMax=1G`,
  `MemorySwapMax=0`) — jt9 mmaps ~60 MB RSS per concurrent child (≤2 channels)
  — and harden the filesystem (`ProtectSystem=strict`, explicit `ReadWritePaths`
  incl. `/var/lib/sigmond`, `ReadOnlyPaths=/etc/meteor-scatter`).
- `MTS-Q-005` `[CODE]` ✅ The SQLite writer SHALL use a single thread-bound
  connection (the cycle-batcher writer thread) for concurrent-safe inserts.
- `MTS-Q-006` `[CODE]` ✅ The callhash table SHALL persist periodically
  (≤5 min) and on shutdown so cumulative resolution survives restarts.
- `MTS-Q-007` `[CODE]` ✅ SHALL honor runtime log level via `--log-level`,
  `METEOR_SCATTER_LOG_LEVEL`, `CLIENT_LOG_LEVEL` (in order), re-applied on
  SIGHUP without restarting RTP streams.
- `MTS-Q-008` `[NEW]` 🟡 The component SHALL run **off radiod's CPU cores** via
  sigmond `AFFINITY_UNITS` so jt9 decode bursts cannot induce RX888 USB drops.
  *(meteor-scatter's presence in `AFFINITY_UNITS` is a sigmond-side obligation —
  verify it was added; this exact regression hit wspr-recorder. gap.)*

## 8. External interfaces

### 8.1 Inputs *(derived from deploy.toml + per-instance config + `inventory --json`)*
- **RF:** radiod MSK144 channels via `ka9q-python` — instance
  `sigma-rx888mk2-status.local`, **2 channels**, `frequencies_hz =
  [28130000, 50260000]` (10 m @ 28.130 MHz, 6 m @ 50.260 MHz), preset `usb`,
  `sample_rate=12000`, `encoding=s16be`.
- **Config:** `/etc/meteor-scatter/<instance>.toml` (preferred) or legacy
  `/etc/meteor-scatter/meteor-scatter-config.toml`. Operator MUST set:
  `[[radiod]].status` (mDNS, never IP); `[radiod.msk144].freqs_hz`. SHOULD set:
  `[station].callsign`/`grid_square` (required to upload). Optional:
  `[paths].decoder_jt9`/`keep_wav`, `[timing].chain_delay_ns`, `[instance]`
  `reporter_id`/`sources`, PSWS IDs.
- **Per-instance env:** `/etc/meteor-scatter/env/<instance>.env` —
  `METEOR_SCATTER_DELIVERY_MODE` (default `direct`), `METEOR_SCATTER_USE_HS_UPLOADER`,
  `METEOR_SCATTER_DIRECT_DEDUP`, `METEOR_SCATTER_LOG_LEVEL`.
- **Coordination/identity:** `/etc/sigmond/coordination.env` —
  `STATION_CALL`/`STATION_GRID`, `SIGMOND_SQLITE_PATH`,
  `RADIOD_<id>_CHAIN_DELAY_NS`, `CLIENT_LOG_LEVEL`.
- **Deps:** `ka9q-python ≥3.14` (PyPI; editable sibling), `callhash`,
  `hs-uploader`, `numpy`; external **`jt9`** binary (bundled); optional
  `whiptail` (apt) for the wizard. *(Note: deploy.toml still declares stale
  `[[deps.git]]` for `ft8_lib`/`decode_ft8` + `ftlib-pskreporter` — see §12.)*
- **Timing authority:** hf-timestd `/run/hf-timestd/authority.json` (read,
  see §8.3).

### 8.2 Outputs *(derived from `inventory --json`)*
- **Local sink:** SQLite target **`msk144.spots`** (`mode=msk144`,
  `table=spots`, `schema_version=2`) at `/var/lib/sigmond/sink.db` (or
  `SIGMOND_SQLITE_PATH`). Row fields: `time`, `mode`, `decoder_kind`,
  `snr_db`, `dt`, `frequency`(+`frequency_mhz`), `message`, `tx_call`,
  `rx_call`, `grid`, `report`, `host_call`/`host_grid`, `radiod_id`,
  `instance`, `reporter_id`, `rx_source`, `frequency_bucket_hz`,
  `processing_version`, `forward_to_pskreporter`, `timing_authority{...}`.
- **Spot logs:** `/var/log/meteor-scatter/<radiod_id>-msk144.log` (file sink,
  `retention_days=365`, `mb_per_day≈5`) — the canonical local artefact, listed
  in `inventory.log_paths`.
- **WAV spool:** `/var/lib/meteor-scatter/<radiod_id>/msk144/` (deleted after
  decode; `retention_days=0`).
- **Upload:** PSKReporter via `hs-uploader`/`PskReporterTcp` (`MTS-F-040`);
  watermark state `/var/lib/hs-uploader/watermarks.db`.
- **Process log:** systemd journal (`SyslogIdentifier=meteor-scatter@%I`).
- Retention is sink-trimmable; storage_trim's `PSK_RETENTION_MIN` policy keys
  on `("psk","spots")` — **`msk144.spots` is not yet covered** (`MTS-F-092`).

### 8.3 Contracts / APIs (reference, not restated)
- `MTS-I-001` `[CODE]` ✅ Conforms to the **client contract** (code declares
  v0.8; deploy.toml `contract_version=0.8`; `deploy.toml package.version=0.4.0`).
  `inventory` declares `templated_units=["meteor-scatter@.service"]`,
  `data_sinks=[file: spool, file: log_dir]`, `data_destination` read from
  `ChannelInfo`, `provides_timing_calibration=false`,
  `uses_timing_calibration=false`. Field semantics: see
  [CLIENT-CONTRACT.md](https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md)
  §3/§7/§17 — not restated here.
- `MTS-I-002` `[CODE]` ✅ **§18 timing authority read-and-stamped for provenance,
  intentionally NOT applied to gate timing:** reads hf-timestd's authority via
  `authority_reader.py`, stamps a `timing_authority` block into every row for
  provenance, and falls back to the standalone marker when absent.
  **Does NOT apply the authority** — `inventory` reports
  `uses_timing_calibration=false`, `timing_authority_applied=null`. meteor-scatter's
  products are MSK144 ~15 s slot-quantized (jt9 MSK144), so RTP-default timing is
  sufficient. This is a deliberate design decision (sigmond #36), not an open gap.
  Subscriber obligations are defined by the contract, not here.
- `MTS-I-003` `[DOC]` ✅ The §14 configuration interview is delegated to
  meteor-scatter's own `config init|edit` argparse subcommands (registered in
  `deploy.toml [contract.config]`); sigmond never edits the TOML.

## 9. Data requirements

The **per-mode log line** (`YYYY/MM/DD HH:MM:SS snr dt abs_freq & message`) is
the on-disk canonical record; the sink row (§8.2) is its parsed,
callhash-resolved, provenance-stamped projection into `msk144.spots`
(`schema_version=2` — the `hs-uploader` reader filters on it, so producer and
reader must match or rows are silently treated as stale-schema). Volume is low
(meteor pings are rare; `mb_per_day≈5` for logs). Retention: spot logs 365 d;
WAV spool 0 d (deleted post-decode); sink rows operator-trimmed (no MSK144 trim
policy yet, `MTS-F-092`). Provenance: every row carries the §18
`timing_authority` block (source/tier/σ/age or standalone marker) and the slot's
RTP-anchored UTC.

## 10. Dependencies & development sequence

**Runtime deps:** `radiod` (required), `ka9q-python`/`callhash`/`hs-uploader`
(editable siblings), `numpy`; the external **`jt9`** decoder (bundled
arch-resolved binary, **external WSJT-X dependency** — meteor-scatter does not
build or own it); optional `whiptail`. Hardware: RX888 via radiod (10 m HF +
6 m, the same path that serves 6 m FT8/FT4 on `sigma` today).

**Development sequence (intended, recovered as requirement — from
METEOR-SCATTER-DESIGN.md §7):**
- **Phase 0** — resolve 6 m hardware path, jt9 MSK144 CLI/output, upload target
  (all done 2026-06-12 except upload-target confirmation).
- **Phase 1 (shipped 2026-06-12)** — scaffold from psk-recorder; strip FT4/FT8,
  collapse to a single 15 s `msk144` mode; greenfield contract surface
  (`inventory`/`validate`/`version`) + placeholder fail-fast + watchdog.
- **Phase 2** — record → 15 s WAV → `jt9 --msk144` decode → parse pings (mirror
  wspr's `DecoderRunner`, decode-timeout bounded). Live decode-column
  validation still open (`MTS-F-090`).
- **Phase 3** — upload heard spots to the confirmed target under the shared
  reporter_id. **Contradictory state:** per-instance config says wsprdaemon.org
  upload is *deferred* (sink-only); recorder code wires *direct PSKReporter*
  (`MTS-F-091`).
- **Phase 4** — hardening pass + tests (~222 collected) against the sweep
  checklist, then live deploy + monitor (live on `sigma` as `@AC0G=S`).

## 11. Acceptance criteria & verification

- Contract conformance → `meteor-scatter validate --json` (exit 0, no `fail`)
  surfaced via `smd status`; `inventory --json` is pure-JSON (verified — runs
  clean, `issues: []`).
- Decode correctness → `MTS-F-013` live MSK144 `decoded.txt` column check (the
  scientific-rigour hinge; currently a pending live-validation item).
- Sink/log integrity → `msk144.spots` row schema (`schema_version=2`) stability
  + graceful no-op when the sink is absent; per-mode log remains canonical.
- Callhash resolution → cross-mode hash substitution (a call seen on FT8/WSPR
  resolves an MSK144 `<NNNNNNN>`), refusing ambiguous slots.
- Resilience → placeholder fail-fast (`exit 78`, no crash-loop), `Type=notify`
  watchdog healthy, RTP-sample-tied liveness (`MTS-F-061` to confirm).
- Standalone operability → `scripts/install.sh` on a radiod-only host runs the
  daemon and writes spots with no sigmond present.

## 12. Risks & open questions

- `MTS-F-090` `[NEW]` 🟡 **Live decode-column validation outstanding:** the
  `decoded.txt` MSK144 parser was confirmed only against an empty/synthetic run;
  the exact column layout (`HHMMSS snr dt freq & message`) MUST be verified
  against a real ping decode before any spot accuracy is claimed.
- `MTS-F-091` `[NEW]` 🟡 **Upload-target contradiction (highest-value gap):** the
  per-instance config (`/etc/meteor-scatter/AC0G=S.toml`) and `recorder.py`
  module docstring say wsprdaemon.org upload is *deferred / sink-only*, while
  `recorder._start_uploaders` + `hs_uploader_shim` wire **direct PSKReporter**
  (default `DELIVERY_MODE=direct`). The original operator intent
  (METEOR-SCATTER-DESIGN.md) was **wsprdaemon.org under the shared `AC0G=S`
  reporter_id**. Decide and reconcile: PSKReporter vs wsprdaemon, and whether
  upload is live or deferred. *(candidate #18 Clients issue.)*
- `MTS-F-092` `[NEW]` ⬜ **No `msk144.spots` trim policy:** sigmond
  `storage_trim` keys retention on `("psk","spots")`; `msk144.spots` has no
  per-target policy, so rows accumulate unbounded under `--all`. Add an
  `MSK144_RETENTION_MIN` target.
- `MTS-F-093` `[NEW]` 🟡 **Pervasive psk-skeleton doc drift:** `README.md`
  (FT4/FT8 + `decode_ft8` + `pskreporter-sender`), `deploy.toml`
  (`[[deps.git]]` ft8_lib/ftlib-pskreporter; `[client_features]` verbs `psk`,
  "FT4/FT8 spot channels"), and `docs/SIGMOND-CONTRACT.md` (v0.4, FT8/FT4
  inventory, `decode_ft8`) all describe the **parent** psk-recorder, not the
  MSK144 reality. SHALL be rewritten or marked. Highest doc-debt in the suite.
- `MTS-F-094` `[NEW]` 🟡 **Version/contract skew:** `pyproject` 0.1.0 vs
  `deploy.toml package.version` 0.4.0; `contract.py`/`deploy` say contract 0.8
  while `CLAUDE.md` says 0.7 and `SIGMOND-CONTRACT.md` says 0.4. Align to one set.
- `MTS-F-095` `[NEW]` ⬜ **Legacy `pskreporter` binary check:** `validate` (per
  CLAUDE.md) may still check for `/usr/local/bin/pskreporter` though the
  subprocess is gone (the upload is in-process). Retire the check.
- `MTS-Q-008` (§7) **AFFINITY_UNITS membership** unverified — confirm
  meteor-scatter is pinned off radiod cores sigmond-side.
- `MTS-F-061` (§6.7) RTP-sample-tied watchdog gate presence unverified in this
  build.

## 13. Traceability

| Requirement | #18 issue | Verification | PSWS #6 |
|---|---|---|---|
| MTS-F-010/012 (jt9 MSK144 decode) | Clients: meteor-scatter | live decode + log-line check | #6:31 (sensor integ.) |
| MTS-F-030 (msk144.spots sink) | Clients: meteor-scatter | sink schema test (schema_version=2) | #6:31 |
| MTS-F-040/041 (PSKReporter upload) | *(new — file)* | smd verifier / PSKReporter ack | — |
| MTS-F-021 (callhash resolution) | — | cross-mode hash-substitution test | — |
| MTS-F-090 (decode-column validation) | *(new — file)* | real-ping decode fixture | — |
| MTS-F-091 (upload-target contradiction) | *(new — file)* | config/code reconcile | — |
| MTS-F-092 (msk144 trim policy) | *(new — file)* | storage_trim target test | — |
| MTS-F-093 (psk-skeleton doc drift) | *(new — file)* | doc rewrite review | — |
| MTS-I-002 (§18 consumer, read-only) | superdarn/timing parity | timing-authority stamp test | #6:50 |

*New rows (MTS-F-090/091/092/093/094, MTS-Q-008) are this review's surfaced
gaps; promote to the #18 meteor-scatter epic. MTS-F-091 (upload-target
contradiction) and MTS-F-093 (doc drift) are the two highest-value.*
