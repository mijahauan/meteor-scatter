"""Client-contract v0.8 inventory and validate JSON builders."""

from __future__ import annotations

import logging
import os
import shutil
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

from meteor_scatter.config import (
    get_freqs,
    get_mode_params,
    load_config,
    resolve_radiod_status,
    is_placeholder_status,
)
from meteor_scatter.version import GIT_INFO

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "0.8"


def build_inventory(config: dict, config_path: Path) -> dict:
    """Build the inventory --json payload per contract v0.8."""
    station = config.get("station", {})
    paths = config.get("paths", {})
    log_dir = paths.get("log_dir", "/var/log/meteor-scatter")

    try:
        version = pkg_version("meteor-scatter")
    except Exception:
        version = "0.1.0"

    radiod_blocks = config.get("radiod", [])
    if isinstance(radiod_blocks, dict):
        radiod_blocks = [radiod_blocks]

    instances = []
    all_log_paths: dict[str, Any] = {}

    for block in radiod_blocks:
        # Canonical mDNS multicast name per RADIOD-IDENTIFICATION.md
        # §3.1.  Phase 6 cutover removed the legacy `id` field; the
        # status name IS the identifier.
        status_dns = resolve_radiod_status(block)
        # Internal label for env-var keys and per-instance file paths.
        # Derived from the status name (sanitized: dots → underscores
        # for env-var compatibility).
        radiod_id = status_dns
        msk144_freqs = get_freqs(block, "msk144")
        all_freqs = sorted(set(msk144_freqs))

        # Inventory `radiod_id` field is the multicast name (the only
        # functional identifier per RADIOD-IDENTIFICATION.md §3.2).
        inventory_radiod_id = status_dns

        chain_delay_env = f"RADIOD_{radiod_id.upper().replace('-', '_').replace('.', '_')}_CHAIN_DELAY_NS"
        chain_delay_raw = os.environ.get(chain_delay_env)
        chain_delay = int(chain_delay_raw) if chain_delay_raw else None

        modes = ["msk144"] if msk144_freqs else []

        spool_path = f"{paths.get('spool_dir', '/var/lib/meteor-scatter')}/{radiod_id}"

        # CONTRACT v0.6 §17 — output sinks per instance.  meteor-scatter
        # writes spots into sigmond's local SQLite sink (via the
        # in-process tailer) and to per-mode log files; both are file
        # sinks from the contract's point of view.
        data_sinks: list[dict[str, Any]] = [
            {
                "kind":           "file",
                "target":         spool_path,
                "schema_ref":     None,
                "retention_days": 0,
                "mb_per_day":     0,
            },
            {
                "kind":           "file",
                "target":         log_dir,
                "schema_ref":     None,
                "retention_days": 365,
                "mb_per_day":     5,
            },
        ]

        instance = {
            "instance": radiod_id,
            "radiod_id": inventory_radiod_id,
            "host": "localhost",
            "radiod_status_dns": status_dns,
            "data_destination": None,
            "ka9q_channels": len(msk144_freqs),
            "frequencies_hz": all_freqs,
            "modes": modes,
            "data_sinks": data_sinks,
            "uses_timing_calibration": False,
            "provides_timing_calibration": False,
            "chain_delay_ns_applied": chain_delay,
            # CONTRACT v0.7 §18 — runtime-state field for the §18
            # subscription. meteor-scatter runs in RTP-default mode (PSK
            # decoding is ms-tolerant; no hard-deadline scheduling
            # against UTC, so subscribing to a peer authority would
            # not improve spot quality). Reported as null to satisfy
            # the v0.7 inventory shape and signal "contract-aware,
            # currently default mode."
            "timing_authority_applied": None,
        }
        instances.append(instance)

        # The process log goes to the systemd journal
        # (StandardOutput=journal) — see it via `smd log meteor-scatter`.
        # log_paths lists only file-based logs (for `smd log --files`).
        instance_logs: dict[str, Any] = {}
        spot_logs: dict[str, str] = {}
        if msk144_freqs:
            spot_logs["msk144"] = f"{log_dir}/{radiod_id}-msk144.log"
        if spot_logs:
            instance_logs["spots"] = spot_logs
        all_log_paths[radiod_id] = instance_logs

    effective_level = logging.getLogger().getEffectiveLevel()
    log_level_name = logging.getLevelName(effective_level)

    payload: dict[str, Any] = {
        "client": "meteor-scatter",
        "version": version,
        "contract_version": CONTRACT_VERSION,
        "config_path": str(config_path),
    }

    if GIT_INFO:
        payload["git"] = GIT_INFO

    if all_log_paths:
        payload["log_paths"] = all_log_paths

    payload["log_level"] = log_level_name
    payload["instances"] = instances
    payload["deps"] = {
        "git": [
            {"name": "ka9q-radio", "note": "jt9 --msk144 decoder (bundled in-repo)"},
        ],
        "pypi": [
            {"name": "ka9q-python", "version": ">=3.6.0"},
        ],
    }
    payload["issues"] = _collect_issues(config, paths)

    return payload


def build_validate(config: dict, config_path: Path | None = None) -> dict:
    """Build the validate --json payload per contract v0.8.

    §12.3: report the absolute path of the loaded config.
    """
    paths = config.get("paths", {})
    issues = _collect_issues(config, paths)
    payload: dict[str, Any] = {
        "ok": not any(i["severity"] == "fail" for i in issues),
    }
    if config_path is not None:
        payload["config_path"] = str(config_path)
    payload["issues"] = issues
    return payload


def _collect_issues(config: dict, paths: dict) -> list[dict]:
    """Run validation checks and return issues list."""
    issues: list[dict] = []

    station = config.get("station", {})
    if not station.get("callsign"):
        issues.append({
            "severity": "warn",
            "instance": "all",
            "message": "station.callsign is empty",
        })
    if not station.get("grid_square"):
        issues.append({
            "severity": "warn",
            "instance": "all",
            "message": "station.grid_square is empty",
        })

    # The jt9 MSK144 decoder is bundled in-repo and arch-resolved at runtime;
    # only validate an explicit override (paths.decoder / paths.decoder_jt9).
    decoder = paths.get("decoder_jt9") or paths.get("decoder") or ""
    if decoder and not shutil.which(decoder) and not Path(decoder).is_file():
        issues.append({
            "severity": "warn",
            "instance": "all",
            "message": f"decoder override not found: {decoder}",
        })

    radiod_blocks = config.get("radiod", [])
    if isinstance(radiod_blocks, dict):
        radiod_blocks = [radiod_blocks]
    if not radiod_blocks:
        issues.append({
            "severity": "fail",
            "instance": "all",
            "message": "no [[radiod]] blocks configured",
        })

    for block in radiod_blocks:
        rid = block.get("status", "<unnamed>")
        if not block.get("status"):
            issues.append({
                "severity": "fail",
                "instance": rid,
                "message": (
                    "[[radiod]] block has no `status` field "
                    "(mDNS multicast name)"
                ),
            })
        elif is_placeholder_status(block.get("status")):
            issues.append({
                "severity": "fail",
                "instance": rid,
                "message": (
                    "[[radiod]] `status` is the unconfigured placeholder "
                    f"{block.get('status')!r} — run `meteor-scatter config "
                    "init` to set the real radiod mDNS status name"
                ),
            })

        msk144 = get_freqs(block, "msk144")
        if not msk144:
            issues.append({
                "severity": "warn",
                "instance": rid,
                "message": "no MSK144 frequencies configured",
            })

        # §12.2 (v0.4): SSRC uniqueness. Duplicate
        # (freq, preset, sample_rate, encoding) tuples collide on
        # SSRC; MultiStream's slot dict silently overwrites.
        seen: dict[tuple, str] = {}
        for mode in ("msk144",):
            params = get_mode_params(block, mode)
            for hz in get_freqs(block, mode):
                key = (int(hz), params["preset"], params["sample_rate"], params["encoding"])
                if key in seen:
                    issues.append({
                        "severity": "fail",
                        "instance": rid,
                        "message": (
                            f"SSRC collision: {mode.upper()} {hz} Hz "
                            f"duplicates {seen[key]} "
                            f"(preset={params['preset']}, "
                            f"rate={params['sample_rate']}, "
                            f"enc={params['encoding']}) — "
                            f"ka9q-python will silently drop one"
                        ),
                    })
                else:
                    seen[key] = f"{mode.upper()} {hz} Hz"

    return issues
