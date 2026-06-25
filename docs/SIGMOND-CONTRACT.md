# Sigmond client contract conformance

meteor-scatter implements the [HamSCI client contract][contract] (v0.4),
maintained in the sigmond repository at
[`docs/CLIENT-CONTRACT.md`][contract]. It is the contract's
**greenfield v0.3 reference implementation** (per §9) and surfaced
all six v0.4 hardening items (§12) during its Phase 1 deploy.

This document is a section-by-section map of how meteor-scatter
satisfies each contract surface. The contract itself is the
authoritative spec; this is the implementation index.

## What the contract is for

Sigmond is a coordinator across multiple HamSCI clients running on
the same station (hf-timestd, wsprdaemon, meteor-scatter, ka9q-web).
The contract is the *only* interface between sigmond and a client:

- Sigmond never imports client code, never edits client config files,
  never shells into a client.
- Every client must run **standalone with no sigmond present**.
- When sigmond is present, it learns about a client by shelling
  `<client> inventory --json` and `<client> validate --json`, and it
  influences a client only by writing `/etc/sigmond/coordination.env`
  and per-unit drop-ins in the client's `<unit>.d/` namespace.

A client is contract-conformant if the same binary runs unchanged
under both regimes.

## §1 — Native config

Lives at `/etc/meteor-scatter/meteor-scatter-config.toml`. Schema is
meteor-scatter's own — see [CONFIG.md](CONFIG.md). Cross-station
concerns (chain-delay correction, log level) come from sigmond's
coordination.env, not from this file.

## §2 — Binding to radiod by id

Each `[[radiod]]` block in the config names its upstream radiod by an
`id` field and a `radiod_status` mDNS hostname:

```toml
[[radiod]]
id            = "bee1-rx888"
radiod_status = "bee1-status.local"
```

Sigmond may override the status name at runtime by setting
`RADIOD_BEE1_RX888_STATUS=...` in coordination.env. meteor-scatter
reads this in [src/meteor_scatter/config.py:107](../src/meteor_scatter/config.py)
(`resolve_radiod_status`) before falling back to the config field.
Standalone deployments work without the env var.

## §3 — Self-describe CLI

Three JSON subcommands. All three keep stdout pristine — the CLI
installs a guard at the top of `main()` that redirects the root
logger to stderr before parsing args, so `inventory --json | jq` never
chokes on a stray banner line.

```bash
meteor-scatter inventory --json
meteor-scatter validate  --json
meteor-scatter version   --json
```

`inventory --json` shape (representative):

```json
{
  "client": "meteor-scatter",
  "version": "0.1.0",
  "contract_version": "0.4",
  "config_path": "/etc/meteor-scatter/meteor-scatter-config.toml",
  "git": {"sha": "...", "short": "...", "ref": "main", "dirty": false},
  "log_paths": {
    "bee1-rx888": {
      "spots": {
        "ft8": "/var/log/meteor-scatter/bee1-rx888-ft8.log",
        "ft4": "/var/log/meteor-scatter/bee1-rx888-ft4.log"
      }
    }
  },
  "log_level": "INFO",
  "instances": [
    {
      "instance": "bee1-rx888",
      "radiod_id": "bee1-rx888",
      "radiod_status_dns": "bee1-status.local",
      "data_destination": "239.7.245.164",
      "ka9q_channels": 20,
      "frequencies_hz": [...],
      "modes": ["ft8", "ft4"],
      "disk_writes": [...],
      "uses_timing_calibration": false,
      "provides_timing_calibration": false,
      "chain_delay_ns_applied": 0
    }
  ],
  "deps": {"git": [...], "pypi": [...]},
  "issues": []
}
```

Builders are in [src/meteor_scatter/contract.py](../src/meteor_scatter/contract.py).

## §4 — Systemd units

Templated unit `meteor-scatter@.service` with `%i` matching the
`[[radiod]].id`. Sources both the sigmond coordination env and an
optional per-instance env file, both with the leading dash so the
unit runs without sigmond installed:

```ini
EnvironmentFile=-/etc/sigmond/coordination.env
EnvironmentFile=-/etc/meteor-scatter/env/%i.env
```

Sigmond is welcome to drop CPU-affinity files at
`/etc/systemd/system/meteor-scatter@<id>.service.d/10-sigmond-cpu-affinity.conf`;
meteor-scatter writes nothing under that path itself. Full unit at
[systemd/meteor-scatter@.service](../systemd/meteor-scatter@.service).

## §5 — Deploy manifest

[`deploy.toml`](../deploy.toml) at the repo root declares build
steps, install steps, the systemd unit list, and external deps
(`ka9q-radio` for `decode_ft8`, `ftlib-pskreporter` for
`pskreporter-sender`, `ka9q-python` from PyPI). Sigmond uses this to
install/upgrade meteor-scatter without carrying any meteor-scatter-specific
knowledge in its own code.

The standalone-safe equivalent is `scripts/install.sh` — same
production layout, no sigmond required.

## §6 — Talking to radiod

meteor-scatter talks to `radiod` exclusively through `ka9q-python`'s
`RadiodControl`. It never speaks the radiod control protocol
directly. See [src/meteor_scatter/core/recorder.py](../src/meteor_scatter/core/recorder.py)
(`_provision_channels`).

## §7 — Deterministic data multicast destination (v0.3)

meteor-scatter calls `RadiodControl.ensure_channel(...)` **without**
passing `destination=`. `ka9q-python` derives the multicast group per
client identity and returns the resolved address in `ChannelInfo`.
meteor-scatter reads it from `ChannelInfo.destination` for the
`data_destination` field in `inventory --json` but never selects or
computes it.

There is no `data_destination` key in meteor-scatter's config schema —
operator overrides go in radiod config or ka9q-python configuration,
not here.

## §8 — Radiod-scoped facts: chain delay

On startup and on SIGHUP, meteor-scatter reads
`RADIOD_<ID>_CHAIN_DELAY_NS` from the environment and surfaces the
value in `inventory --json` as `chain_delay_ns_applied`. The
standalone fallback is `[timing].chain_delay_ns` in the config.

meteor-scatter is **not** the calibrator (that's hf-timestd) and does
not currently apply the correction to its sample-to-UTC conversion —
spot timestamps are accurate to ~1 second from FT8/FT4 slot
quantization, well outside the chain-delay regime. The contract hook
is in place so a future tightening (sub-second timestamping for, say,
millisecond-accurate skew studies) requires no contract-level work.

## §10 — Logging discipline

- The process log goes to the systemd journal via the unit's
  `StandardOutput=journal` (`SyslogIdentifier=meteor-scatter@<id>`) —
  query it with `journalctl -u meteor-scatter@<id>` or `smd log
  meteor-scatter`. It is journal-only; there is no per-instance process
  log file.
- Spot logs go to `/var/log/meteor-scatter/<radiod_id>-{ft8,ft4}.log`.
- The file-based spot-log paths are surfaced in `inventory --json`
  under the top-level `log_paths` object, keyed by radiod id (since
  one `meteor-scatter` install can host multiple instances). The
  journal-only process log is not listed there.

## §11 — Runtime log level

meteor-scatter honors:

1. `--log-level <LEVEL>` CLI flag
2. `METEOR_SCATTER_LOG_LEVEL` env var (sigmond-published)
3. `CLIENT_LOG_LEVEL` env var (sigmond generic fallback)
4. Default: `INFO`

A SIGHUP handler in the daemon's main loop re-reads (2) and (3) and
re-applies the level to the root logger without restarting RTP
streams. `smd log --level=DEBUG meteor-scatter` is therefore a one-step
operation.

Resolution code: [src/meteor_scatter/cli.py:22-34](../src/meteor_scatter/cli.py).

## §12 — Validate hardening (v0.4)

The six items in §12 were surfaced by meteor-scatter's own Phase 1
deploy on 2026-04-13. Status of each in meteor-scatter:

### §12.1 Entry-point reachability (MUST) — implemented

`cli.py` has the `if __name__ == "__main__": main()` guard. Added in
[`520e39f`][520e39f] after the unit's `python -m meteor_scatter.cli`
silently no-op'd because the guard was missing.

### §12.2 SSRC uniqueness (MUST) — implemented

`validate` rejects configs where two channels in the same
`[[radiod]]` block share `(freq, preset, sample_rate, encoding)`.
Surfaced when an FT4 entry on 1.840 MHz collided with the FT8 entry
on the same dial — `ka9q-python`'s `MultiStream` keys callbacks by
SSRC and silently overwrote one. See [`be4a050`][be4a050].

### §12.3 Config path disclosure (MUST) — implemented

`config_path` is a top-level field in both `inventory --json` and
`validate --json`, holding the absolute path of the file actually
loaded after env-var overrides and CLI flag resolution. Eliminates
"I edited the config and nothing changed" — the running daemon's
inventory output names the file it read.

### §12.4 Decoder-spool mutation (SHOULD) — documented

`decode_ft8` unconditionally unlinks the WAV it just decoded. With
`keep_wav = true`, meteor-scatter still skips its own unlink, but the
WAV is gone before it returns from `wait()` because the decoder
deleted it first. To retain WAVs for debugging, snapshot the file
*before* forking (e.g. by holding a hardlink in a separate dir under
`ReadWritePaths`). This is documented in
[OPERATIONS.md](OPERATIONS.md#debugging-with-keep_wav).

### §12.5 Pattern A canonical layout (SHOULD) — implemented

Repo lives at `/opt/git/sigmond/meteor-scatter` (group-readable by the service
user `meteorscat`). `scripts/install.sh` enforces this and verifies
traversability with a `sudo -u meteorscat test -r ...` check. The
anti-pattern (`/opt/git/sigmond/...` as a symlink to `~/git/...`) is rejected
by the install script — the service user can't traverse a mode-700
home.

### §12.6 ka9q-python PyPI-lag check (SHOULD) — pending

Not yet implemented. `validate` should warn when the installed
`ka9q_python.__version__` is older than the minimum declared in
`pyproject.toml` (currently `>=3.8.0`). Tracked as a v0.1.1 retrofit
item.

## What sigmond promises in return

(From the contract; informational here.)

- Never edits `/etc/meteor-scatter/meteor-scatter-config.toml`.
- Reads inventory output to learn what meteor-scatter wants.
- Publishes per-radiod facts and per-client log levels in
  `coordination.env`, atomic on each `smd apply`.
- Writes CPU affinity drop-ins only at
  `/etc/systemd/system/meteor-scatter@<id>.service.d/10-sigmond-cpu-affinity.conf`.
- Sends SIGHUP after rewriting log levels.
- Never depends on meteor-scatter code or shells out to `meteor-scatter`
  for anything beyond `inventory --json` and `validate --json`.

## Versioning

meteor-scatter reports `contract_version` in inventory output. Bump
when adopting a new contract version after auditing the changelog at
the top of the canonical doc.

| meteor-scatter release | contract version | Notes |
|---|---|---|
| 0.1.0 | 0.3 | Greenfield v0.3 reference. |
| 0.1.0 (current) | 0.4 | §12 retrofit landed at [`b5eb378`][b5eb378] (config_path + SSRC uniqueness check). |

[contract]: https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md
[520e39f]: https://github.com/HamSCI/meteor-scatter/commit/520e39f
[be4a050]: https://github.com/HamSCI/meteor-scatter/commit/be4a050
[b5eb378]: https://github.com/HamSCI/meteor-scatter/commit/b5eb378
