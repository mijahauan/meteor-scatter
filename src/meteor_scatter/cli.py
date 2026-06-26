"""meteor-scatter CLI entry point.

Subcommands:
    inventory   — contract v0.8 JSON inventory
    validate    — contract v0.8 config validation
    version     — version + git block
    daemon      — long-running recorder (Phase 1)
    status      — health check (Phase 1)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path


def _resolve_log_level() -> int:
    """Resolve log level per contract v0.3 §11 precedence.

    1. --log-level CLI flag (handled by caller, not here)
    2. METEOR_SCATTER_LOG_LEVEL env var
    3. CLIENT_LOG_LEVEL env var
    4. Default: INFO
    """
    for env_key in ("METEOR_SCATTER_LOG_LEVEL", "CLIENT_LOG_LEVEL"):
        val = os.environ.get(env_key, "").upper().strip()
        if val and hasattr(logging, val):
            return getattr(logging, val)
    return logging.INFO


def _install_sighup_handler() -> None:
    """Re-read log level from env on SIGHUP (contract v0.3 §11)."""
    def _on_sighup(signum, frame):
        level = _resolve_log_level()
        logging.getLogger().setLevel(level)
        logging.getLogger(__name__).info(
            "SIGHUP: log level set to %s", logging.getLevelName(level)
        )
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _on_sighup)


def main():
    # "Quiet" surfaces emit clean stdout (JSON or shell-parseable) and
    # must not get the "meteor-scatter starting" log line on top.
    # config show / config apply, env show / env apply join inventory /
    # validate / version because the whiptail wizard parses their stdout.
    _contract_quiet = any(
        arg in ("inventory", "validate", "version")
        for arg in sys.argv[1:3]
    ) or (
        len(sys.argv) >= 3 and sys.argv[1] in ("config", "env")
        and sys.argv[2] in ("show", "apply")
    )

    root = logging.getLogger()
    if _contract_quiet:
        root.setLevel(logging.WARNING)
    else:
        root.setLevel(_resolve_log_level())

    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        # Include ISO-8601 timestamp so off-line log scrapers (e.g.
        # sigmond's decode-health collector) can anchor events in
        # time.  systemd's StandardOutput=append:<file> writes raw
        # stdout/stderr to the file with no timestamp prefix; without
        # %(asctime)s every line is a timeless string.
        handler.setFormatter(
            logging.Formatter(
                fmt='%(asctime)s.%(msecs)03dZ %(levelname)s:%(name)s:%(message)s',
                datefmt='%Y-%m-%dT%H:%M:%S',
            )
        )
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            if _contract_quiet:
                handler.setLevel(logging.WARNING)

    if not _contract_quiet:
        logging.info("meteor-scatter starting")

    parser = argparse.ArgumentParser(
        prog="meteor-scatter",
        description="MSK144 meteor-scatter spot recorder and wsprdaemon.org uploader",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Shared arguments added to every subparser
    def _add_common(sub):
        sub.add_argument(
            "--config", type=Path, default=None,
            help="Path to meteor-scatter-config.toml",
        )
        sub.add_argument(
            "--log-level", default=None,
            help="Override log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
        )

    sub_inv = subparsers.add_parser("inventory", help="Contract v0.8 inventory")
    sub_inv.add_argument("--json", action="store_true", default=True)
    _add_common(sub_inv)

    sub_val = subparsers.add_parser("validate", help="Contract v0.8 validation")
    sub_val.add_argument("--json", action="store_true", default=True)
    _add_common(sub_val)

    sub_ver = subparsers.add_parser("version", help="Version info")
    sub_ver.add_argument("--json", action="store_true", default=True)
    _add_common(sub_ver)

    sub_daemon = subparsers.add_parser("daemon", help="Run recorder daemon")
    sub_daemon.add_argument(
        "--instance", default=None,
        help="Reporter-ID instance (loads /etc/meteor-scatter/<instance>.toml "
             "when present; falls back to shared config otherwise). "
             "See sigmond's MULTI-INSTANCE-ARCHITECTURE.md §6.",
    )
    sub_daemon.add_argument(
        "--radiod-id", default=None,
        help="ID of the [[radiod]] block to use (legacy single-source "
             "selector; ignored when --instance resolves to a per-instance "
             "config).",
    )
    _add_common(sub_daemon)

    sub_status = subparsers.add_parser("status", help="Health check")
    _add_common(sub_status)

    # Configuration interview (CONTRACT-v0.5 §14).
    sub_cfg = subparsers.add_parser(
        "config",
        help="initialize or edit meteor-scatter configuration",
    )
    cfg_sub = sub_cfg.add_subparsers(dest="config_command")

    sub_init = cfg_sub.add_parser(
        "init", help="write a fresh meteor-scatter-config.toml from template")
    sub_init.add_argument("--reconfig", action="store_true",
                          help="overwrite existing config")
    sub_init.add_argument("--non-interactive", action="store_true",
                          help="use env-var defaults, do not prompt")
    _add_common(sub_init)

    sub_edit = cfg_sub.add_parser(
        "edit", help="review and update an existing config")
    sub_edit.add_argument("--non-interactive", action="store_true",
                          help="show current values, do not prompt")
    sub_edit.add_argument("--radiod-id", default=None,
                          help="focus edits on a specific [[radiod]] block")
    _add_common(sub_edit)

    # `config show` / `config apply` exist for the whiptail wizard
    # (scripts/config-wizard.sh) and any other tooling that wants to
    # round-trip the config as JSON through the same validator the
    # daemon uses.
    from meteor_scatter import configurator as _cfg
    _cfg.add_show_apply_subparsers(cfg_sub, common=_add_common)

    # Top-level `env show` / `env apply` for per-instance env files at
    # /etc/meteor-scatter/env/<radiod_id>.env -- start-time knobs the
    # systemd unit's EnvironmentFile= consumes.  No managed keys yet in
    # this deposit-only build; the wsprdaemon.org upload knobs arrive
    # with the upload transport (Phase 3).
    _cfg.add_env_subparsers(subparsers, common=_add_common)

    args = parser.parse_args()

    if args.log_level and not _contract_quiet:
        level_name = args.log_level.upper()
        if hasattr(logging, level_name):
            root.setLevel(getattr(logging, level_name))

    if args.command == "inventory":
        _handle_inventory(args)
    elif args.command == "validate":
        _handle_validate(args)
    elif args.command == "version":
        _handle_version(args)
    elif args.command == "daemon":
        _handle_daemon(args)
    elif args.command == "status":
        _handle_status(args)
    elif args.command == "config":
        _handle_config(args)
    elif args.command == "env":
        _handle_env(args)
    else:
        parser.print_help()
        sys.exit(1)


def _handle_env(args):
    from meteor_scatter import configurator
    sub = getattr(args, "env_command", None)
    if sub == "show":
        sys.exit(configurator.cmd_env_show(args))
    if sub == "apply":
        sys.exit(configurator.cmd_env_apply(args))
    print("usage: meteor-scatter env {show|apply} --instance <radiod_id>")
    sys.exit(2)


def _handle_config(args):
    from meteor_scatter import configurator

    sub = getattr(args, "config_command", None)
    if sub == "init":
        sys.exit(configurator.cmd_config_init(args))
    if sub == "edit":
        sys.exit(configurator.cmd_config_edit(args))
    if sub == "show":
        sys.exit(configurator.cmd_config_show(args))
    if sub == "apply":
        sys.exit(configurator.cmd_config_apply(args))
    print("usage: meteor-scatter config {init|edit|show|apply} [...]")
    sys.exit(2)


def _handle_inventory(args):
    from meteor_scatter.config import DEFAULT_CONFIG_PATH, load_config
    from meteor_scatter.contract import build_inventory

    config_path = args.config or Path(
        os.environ.get("METEOR_SCATTER_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    try:
        config = load_config(config_path)
    except Exception as exc:
        # The contract requires `inventory` to always emit valid JSON with
        # structured issues — never crash.  A config that is missing OR
        # present-but-unreadable (e.g. mode 0640 owned by the service user
        # while sigmond probes as the operator) both land here.
        if isinstance(exc, FileNotFoundError):
            msg = f"config not found: {config_path}"
        elif isinstance(exc, OSError):
            msg = f"config unreadable: {config_path} ({exc.strerror or exc})"
        else:
            msg = f"config invalid: {config_path}: {exc}"
        payload = {
            "client": "meteor-scatter",
            "version": "0.1.0",
            "contract_version": "0.4",
            "config_path": str(config_path),
            "instances": [],
            "issues": [
                {
                    "severity": "fail",
                    "instance": "all",
                    "message": msg,
                }
            ],
        }
        print(json.dumps(payload, indent=2))
        return

    payload = build_inventory(config, config_path)
    print(json.dumps(payload, indent=2))


def _handle_validate(args):
    from meteor_scatter.config import DEFAULT_CONFIG_PATH, load_config
    from meteor_scatter.contract import build_validate

    config_path = args.config or Path(
        os.environ.get("METEOR_SCATTER_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    try:
        config = load_config(config_path)
    except Exception as exc:
        # See _handle_inventory: degrade gracefully on a missing OR
        # unreadable config rather than crashing.
        if isinstance(exc, FileNotFoundError):
            msg = f"config not found: {config_path}"
        elif isinstance(exc, OSError):
            msg = f"config unreadable: {config_path} ({exc.strerror or exc})"
        else:
            msg = f"config invalid: {config_path}: {exc}"
        payload = {
            "ok": False,
            "config_path": str(config_path),
            "issues": [
                {
                    "severity": "fail",
                    "instance": "all",
                    "message": msg,
                }
            ],
        }
        print(json.dumps(payload, indent=2))
        sys.exit(1)

    payload = build_validate(config, config_path)
    print(json.dumps(payload, indent=2))
    if not payload["ok"]:
        sys.exit(1)


def _handle_version(args):
    from meteor_scatter import __version__
    from meteor_scatter.version import GIT_INFO

    payload = {
        "client": "meteor-scatter",
        "version": __version__,
    }
    if GIT_INFO:
        payload["git"] = GIT_INFO
    print(json.dumps(payload, indent=2))


def _handle_daemon(args):
    _install_sighup_handler()
    logger = logging.getLogger("meteor_scatter.daemon")

    from meteor_scatter.config import (
        DEFAULT_CONFIG_PATH,
        ensure_sources,
        extract_reporter_id,
        load_config,
        resolve_config_path,
        resolve_radiod_block,
        RADIOD_STATUS_PLACEHOLDER,
        is_placeholder_status,
    )
    from meteor_scatter.core.recorder import MeteorScatterRecorder

    # Phase-3 config resolution (sigmond's MULTI-INSTANCE-ARCHITECTURE.md
    # §4): prefer per-instance config when --instance is given and the
    # file exists; fall back to legacy shared with a deprecation
    # warning otherwise.  --config still wins over both (operator
    # override).
    config_path = resolve_config_path(
        instance=args.instance, explicit_path=args.config,
    )
    config = load_config(config_path)

    # Per-instance config carries the reporter_id in its [instance]
    # block; legacy shared config has None here, and we deliberately
    # do NOT fall back to args.instance.  During the cutover, args.instance
    # is the systemd %i which is typically a radiod identifier
    # (e.g. "my-rx888"), not a reporter ID — using it as a reporter_id
    # would propagate a misleading value into spot rows.  Instead we
    # leave reporter_id=None; ChTailer's row-construction layer falls
    # back to radiod_id (the existing legacy `instance` field's
    # semantic), keeping the field present without claiming it's a
    # real reporter ID.  Operators set a real reporter_id by populating
    # the [instance] block in the per-instance config (sigmond Phase 8
    # `smd instance migrate` is the planned interactive setup path).
    reporter_id = extract_reporter_id(config)

    radiod_block: dict | None = None
    if args.radiod_id is not None:
        # Legacy single-source mode — operator explicitly selected one
        # block; honor it exactly even if the config has more.  Used
        # by ``meteor-scatter@<radiod-id>.service`` template units.
        try:
            radiod_block = resolve_radiod_block(config, args.radiod_id)
        except ValueError:
            # Post-`smd instance migrate` soft-cutover: the systemd
            # template still passes --radiod-id %i (= reporter ID),
            # which doesn't match the [[radiod]] block's id (= mDNS
            # source label) in the per-instance config.  When --instance
            # was given, the per-instance config unambiguously defines
            # this instance's source list — fall through to multi-source
            # mode (which accepts any block count) and rely on
            # ensure_sources to pick the right block.  Warn so the
            # mismatch stays visible to operators.
            if args.instance is None:
                raise
            logger.warning(
                "--radiod-id=%r did not match any [[radiod]] block in "
                "%s; falling through to per-instance multi-source path "
                "(--instance=%r).  Drop --radiod-id from the systemd "
                "template once all reporters are migrated.",
                args.radiod_id, config_path, args.instance,
            )

    if radiod_block is not None:
        blocks = [radiod_block]
        logger.info(
            "Starting meteor-scatter daemon for radiod %s "
            "(config=%s, reporter_id=%s, single-source mode)",
            radiod_block.get("status", "<unconfigured>"), config_path,
            reporter_id or "<derived>",
        )
    else:
        # Multi-source mode — drive every [[radiod]] block in the
        # config from a single process.  In the per-instance world
        # the per-instance config defines this instance's source list.
        sources = ensure_sources(config)
        if not sources:
            raise SystemExit(
                f"No usable [[radiod]] blocks in {config_path}",
            )
        blocks = [s["radiod_block"] for s in sources]
        logger.info(
            "Starting meteor-scatter daemon for %d radiod source(s): %s "
            "(config=%s, reporter_id=%s)",
            len(blocks),
            ", ".join(s["radiod_id"] for s in sources),
            config_path,
            reporter_id or "<derived>",
        )

    # Fail FAST (not crash-loop) when a radiod status address is still the
    # unconfigured sentinel.  sigmond seeds new configs with
    # RADIOD_STATUS_PLACEHOLDER, which can never resolve — without this the
    # Type=notify daemon aborts in connect(), Restart=always respawns it,
    # and it hammers ~10 restarts before StartLimit lockout.  Exit EX_CONFIG
    # (78) — listed in the unit's RestartPreventExitStatus — so systemd stops
    # cleanly.  A real-but-unreachable radiod is NOT caught here (stays
    # transient -> keep retrying, correct for boot order).
    if any(is_placeholder_status(b.get("status")) for b in blocks):
        logger.error(
            "radiod status address is unconfigured (placeholder %r). Run "
            "`meteor-scatter config init` (or `sudo smd bringup`) to set the "
            "real radiod mDNS status name, then start the service. Exiting "
            "without restart (EX_CONFIG 78).",
            RADIOD_STATUS_PLACEHOLDER,
        )
        sys.exit(78)

    recorder = MeteorScatterRecorder(config, blocks, reporter_id=reporter_id)
    recorder.run()


def _handle_status(args):
    print("meteor-scatter: not running (Phase 1 not yet implemented)")
    sys.exit(2)


if __name__ == "__main__":
    main()
