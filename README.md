# msk144-recorder

FT4/FT8 spot recorder and PSK Reporter uploader for [ka9q-radio][ka9q].
Replaces the native `ft8-record` / `ft8-decode` / `pskreporter@` shell
pipeline with a coordinated Python client that follows the HamSCI
sigmond [client contract][contract] (v0.6).

```
radiod (ka9q-radio)
  │   RTP multicast, one stream per (band, mode) channel
  ▼
msk144-recorder daemon (one per radiod)
  ├─ per-channel: ring buffer → 15s/7.5s slot WAV → fork decode_ft8
  ├─ per-mode log file (decode_ft8 native format)
  ├─ per-mode: pskreporter-sender (UDP or TCP to pskreporter.info)
  └─ per-mode: ChTailer → sigmond.hamsci_sink.Writer → psk.spots
```

msk144-recorder decodes with ka9q/ft8_lib's `decode_ft8`.  Rows tag
themselves via `decoder_kind` in `psk.spots`, and ChTailer parses the
decoder output into `psk.spots` rows.

One `msk144-recorder@<radiod_id>.service` instance per radiod. Each
instance handles all configured FT8 and FT4 frequencies on that
radiod.

## Quickstart

External binaries must be present first:
- `decode_ft8` from [ka9q/ft8_lib][ft8_lib] → `/usr/local/bin/decode_ft8` —
  msk144-recorder's FT4/FT8 decoder.
- `pskreporter-sender` from [pjsg/ftlib-pskreporter][ftlib] → `/usr/local/bin/pskreporter-sender`
- A working `radiod@<id>.service` from [ka9q/ka9q-radio][ka9q]

Then:

```bash
git clone https://github.com/mijahauan/meteor-scatter /opt/git/sigmond/msk144-recorder
sudo /opt/git/sigmond/msk144-recorder/scripts/install.sh   # creates user, venv, config, units
sudo msk144-recorder config edit                           # interactive wizard (whiptail) -- see below
sudo systemctl start msk144-recorder@<radiod_id>
journalctl -fu msk144-recorder@<radiod_id>
```

### Configuration

msk144-recorder's operator-facing config spans **three persistence layers**:

| Layer | Path | Owner | Holds |
|---|---|---|---|
| **TOML config** | `/etc/msk144-recorder/msk144-recorder-config.toml` | msk144-recorder | `[station]`, `[paths]`, `[processing]`, `[timing]`, `[[radiod]]` blocks |
| **Coordination env** | `/etc/sigmond/coordination.env` | sigmond | `STATION_CALL`, `STATION_GRID`, `SIGMOND_SQLITE_PATH`, host-wide identity |
| **Per-instance env** | `/etc/msk144-recorder/env/<radiod_id>.env` | msk144-recorder | `MSK144_DELIVERY_PIPELINES`, `MSK144_USE_HS_UPLOADER`, `MSK144_DIRECT_DEDUP` — the upload destination knobs |

The wizard manages layers 1 and 3; it reads from layer 2 (sigmond's
coordination env) for pre-fills but never writes there.

#### Interactive wizard (default)

When stdout is a TTY and `whiptail` is installed, `msk144-recorder config
init` (first time) and `msk144-recorder config edit` (subsequent) launch
a menu-driven wizard:

```
Station    Call=AC0G  Grid=EM38ww40pk
Paths      spool=/var/lib/msk144-recorder  decoder=decode_ft8
Processing lifetime=6000 frames
Timing     chain_delay=0 ns (sigmond usually overrides)
Radiod     blocks: bee1-rx888
Delivery   pipelines: direct,server-raw (per-instance env)
Edit-TOML  Open raw config in $EDITOR (for freqs_hz lists)
Apply      Review and write changes
Cancel     Discard pending changes and exit
```

Inside a section, Cancel drops back to the menu — effective "back"
navigation.  Each section walks its questions linearly with per-field
help and validation.

- **Station / Paths / Processing / Timing** edit the TOML through
  `config apply`.
- **Radiod** lets you pick an existing `[[radiod]]` block to edit
  (`id`, `radiod_status`) or add a new one.  `freqs_hz` arrays stay
  in the raw TOML — use the **Edit-TOML** menu item for those.
- **Delivery** edits `/etc/msk144-recorder/env/<radiod_id>.env` through
  `env apply`.  Shows `SIGMOND_SQLITE_PATH` from coordination.env
  read-only for context.  Auto-downgrades `direct + server-merge` to
  `direct + server-raw` so the wsprdaemon server doesn't double-post.

Per-key help lives in `config/help.toml`; pre-fills come from
`/etc/sigmond/coordination.env` (`STATION_CALL`, `STATION_GRID`) and
the current TOML / env files.

Same UI pattern mag-recorder uses; see that repo's README for the
basic shape.

#### Headless / scripted

```bash
msk144-recorder config init --non-interactive
```

Renders the template with `STATION_CALL` / `SIGMOND_INSTANCE` /
`SIGMOND_RADIOD_STATUS` env-bag substitutions, no prompts.

#### Hand-edit

```bash
sudoedit /etc/msk144-recorder/msk144-recorder-config.toml
sudoedit /etc/msk144-recorder/env/<radiod_id>.env
```

Operator who values inline comments / formatting should pick this
path; the wizard's `config apply` rewrites the TOML cleanly and
doesn't preserve comments.

#### JSON entry points (for sigmond / other tooling)

```bash
msk144-recorder config show  --json [--defaults]              # → TOML as JSON
msk144-recorder config apply --json -                         # ← stdin JSON, validated, atomic write
msk144-recorder env    show  --json --instance <radiod_id>    # → env file as JSON
msk144-recorder env    apply --json - --instance <radiod_id>  # ← stdin JSON, validated, atomic write
```

`config apply` writes `[station]`, `[paths]`, `[processing]`,
`[timing]`, and `[[radiod]]` (overlay-wins for the radiod list — the
operator's full list replaces the file's list; per-band `freqs_hz`
must be passed back in the payload if you want to preserve them).

`env apply` writes `MSK144_DELIVERY_PIPELINES`, `MSK144_USE_HS_UPLOADER`,
`MSK144_DIRECT_DEDUP`, and the legacy `MSK144_DELIVERY_MODE`.  Keys outside
that set are rejected so a typo doesn't silently land in the env
file.  Setting a key to JSON `null` deletes it.

For ongoing development on a checked-out repo:

```bash
sudo /opt/git/sigmond/msk144-recorder/scripts/deploy.sh         # pip install -e + restart instances
sudo /opt/git/sigmond/msk144-recorder/scripts/deploy.sh --pull  # git pull then deploy
```

For tests (no venv needed):

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md) — full install (deps, multi-radiod, paths, permissions)
- [docs/CONFIG.md](docs/CONFIG.md) — TOML schema reference (every section, every key)
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — running it: logs, monitoring, common failures
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — internals for contributors
- [docs/SIGMOND-CONTRACT.md](docs/SIGMOND-CONTRACT.md) — how msk144-recorder satisfies the HamSCI client contract
- [CLAUDE.md](CLAUDE.md) — development briefing (workflow, conventions)

## What it does and does not

**Does:** receive RTP multicast from `radiod`, slot-align audio to FT8
(15s) or FT4 (7.5s) cadence, write a WAV per slot, fork `decode_ft8`,
append spots to per-mode log files in decode_ft8's native format,
supervise a long-running
`pskreporter-sender` per mode that tails those logs and uploads to
pskreporter.info, and stream parsed rows into `psk.spots` via
`sigmond.hamsci_sink.Writer` (sigmond's local SQLite sink by default).

**Does not:** reimplement the FT8/FT4 decoder, reimplement the
pskreporter protocol, or talk to `radiod` over anything but
[ka9q-python][ka9qpy]. Multicast destination addresses are *resolved
from* radiod, never specified by msk144-recorder.

## License

MIT. See [LICENSE](LICENSE). Author: Michael Hauan, AC0G.

[ka9q]: https://github.com/ka9q/ka9q-radio
[ka9qpy]: https://github.com/mijahauan/ka9q-python
[ft8_lib]: https://github.com/ka9q/ft8_lib
[ftlib]: https://github.com/pjsg/ftlib-pskreporter
[contract]: https://github.com/mijahauan/sigmond/blob/main/docs/CLIENT-CONTRACT.md
