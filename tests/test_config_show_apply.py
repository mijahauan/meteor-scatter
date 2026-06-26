"""Cover the JSON I/O the whiptail wizard depends on.

Same shape as mag-recorder's test_config_show_apply.py (sigmond-integration
HEAD on the mag-recorder repo) -- if/when the show/apply/serialize
machinery moves into a sigmond-provided library these tests carry
over with minimal changes.

meteor-scatter's apply is intentionally narrower than mag-recorder's:
only [station], [paths], [processing] are writable.  [[radiod]] arrays
of tables pass through unchanged from the existing file but cannot
be set via apply.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tomllib
from pathlib import Path

import pytest

from meteor_scatter import configurator
from meteor_scatter.config import DEFAULTS


def _ns(**kw) -> argparse.Namespace:
    base = {"config": None, "defaults": False, "json": True,
            "non_interactive": False, "reconfig": False, "log_level": None,
            "path": "-"}
    base.update(kw)
    return argparse.Namespace(**base)


# ---------- config show -----------------------------------------------------

def test_show_defaults_emits_paths_and_processing(tmp_path: Path, capsys) -> None:
    rv = configurator.cmd_config_show(_ns(config=tmp_path / "nope.toml", defaults=True))
    assert rv == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out.keys()) >= {"paths", "processing"}
    assert out["paths"]["decoder"]      == DEFAULTS["paths"]["decoder"]
    assert out["processing"]["radiod_lifetime_frames"] == DEFAULTS["processing"]["radiod_lifetime_frames"]


def test_show_returns_file_contents_without_defaults(tmp_path: Path, capsys) -> None:
    config = tmp_path / "c.toml"
    config.write_text('[station]\ncallsign = "AC0G"\n')
    rv = configurator.cmd_config_show(_ns(config=config, defaults=False))
    assert rv == 0
    assert json.loads(capsys.readouterr().out) == {"station": {"callsign": "AC0G"}}


def test_show_missing_file_without_defaults_returns_empty(tmp_path: Path, capsys) -> None:
    rv = configurator.cmd_config_show(_ns(config=tmp_path / "nope.toml", defaults=False))
    assert rv == 0
    assert json.loads(capsys.readouterr().out) == {}


# ---------- config apply ----------------------------------------------------

def _apply(payload, tmp_path: Path, *, existing: str = "") -> int:
    """Drive cmd_config_apply with payload as stdin; return exit code."""
    config = tmp_path / "c.toml"
    if existing:
        config.write_text(existing)
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        return configurator.cmd_config_apply(_ns(config=config))
    finally:
        sys.stdin = old_stdin


_FIXTURE_WITH_RADIOD = '''\
[station]
callsign = "AC0G"
grid_square = "EM38ww40pk"

[paths]
spool_dir = "/var/lib/meteor-scatter"
log_dir = "/var/log/meteor-scatter"
keep_wav = false

[processing]
radiod_lifetime_frames = 6000

[[radiod]]
id = "test-rx888"
radiod_status = "test-status.local"

[radiod.msk144]
sample_rate = 12000
preset = "usb"
encoding = "s16be"
freqs_hz = [28130000, 50260000]
'''


def test_apply_writes_station_section(tmp_path: Path) -> None:
    rv = _apply({"station": {"callsign": "K1JT", "grid_square": "FN20"}},
                tmp_path, existing=_FIXTURE_WITH_RADIOD)
    assert rv == 0
    with open(tmp_path / "c.toml", "rb") as f:
        loaded = tomllib.load(f)
    assert loaded["station"]["callsign"]    == "K1JT"
    assert loaded["station"]["grid_square"] == "FN20"


def test_apply_preserves_radiod_blocks(tmp_path: Path) -> None:
    """[[radiod]] passes through untouched even though apply doesn't write it."""
    rv = _apply({"station": {"callsign": "K1JT"}},
                tmp_path, existing=_FIXTURE_WITH_RADIOD)
    assert rv == 0
    with open(tmp_path / "c.toml", "rb") as f:
        loaded = tomllib.load(f)
    assert isinstance(loaded["radiod"], list)
    assert loaded["radiod"][0]["id"] == "test-rx888"
    assert loaded["radiod"][0]["msk144"]["freqs_hz"] == [28130000, 50260000]


# NOTE: an earlier version of this test asserted that [[radiod]] was
# not writable via apply at all.  The wizard now writes radiod
# blocks (with the inline-edit / pick-a-block flow); the per-block
# validation (id + radiod_status required, no duplicate ids, must
# be a list) is covered by test_apply_rejects_radiod_missing_id /
# missing_status / duplicate_ids / not_a_list below.


def test_apply_rejects_unknown_section(tmp_path: Path, capsys) -> None:
    rv = _apply({"bogus": {"x": 1}}, tmp_path)
    assert rv == 2
    assert "not writable" in capsys.readouterr().err.lower()


def test_apply_rejects_wrong_type(tmp_path: Path, capsys) -> None:
    """paths.keep_wav is a bool; a string must be rejected."""
    rv = _apply({"paths": {"keep_wav": "yes"}}, tmp_path)
    assert rv == 2
    assert "expects bool" in capsys.readouterr().err.lower()


def test_apply_rejects_negative_lifetime(tmp_path: Path, capsys) -> None:
    """processing.radiod_lifetime_frames must be a non-negative int."""
    rv = _apply({"processing": {"radiod_lifetime_frames": -1}},
                tmp_path, existing=_FIXTURE_WITH_RADIOD)
    assert rv == 2


def test_apply_is_atomic_part_rename(tmp_path: Path) -> None:
    """The write goes via .part + rename so a crash mid-write leaves
    the old file intact.  After a successful apply, no .part file
    should remain."""
    rv = _apply({"station": {"callsign": "AC0G"}},
                tmp_path, existing='[station]\ncallsign = "OLD"\n')
    assert rv == 0
    assert (tmp_path / "c.toml").exists()
    assert not (tmp_path / "c.toml.part").exists()


def test_apply_rejects_non_object_payload(tmp_path: Path) -> None:
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(["not", "a", "dict"]))
    try:
        rv = configurator.cmd_config_apply(_ns(config=tmp_path / "c.toml"))
    finally:
        sys.stdin = old_stdin
    assert rv == 2


def test_apply_rejects_invalid_json(tmp_path: Path) -> None:
    old_stdin = sys.stdin
    sys.stdin = io.StringIO("this is not json {{ broken")
    try:
        rv = configurator.cmd_config_apply(_ns(config=tmp_path / "c.toml"))
    finally:
        sys.stdin = old_stdin
    assert rv == 2


def test_apply_deep_merges_with_existing(tmp_path: Path) -> None:
    """Partial payloads preserve existing fields the wizard didn't touch."""
    rv = _apply({"station": {"callsign": "K1JT"}},
                tmp_path, existing=_FIXTURE_WITH_RADIOD)
    assert rv == 0
    with open(tmp_path / "c.toml", "rb") as f:
        loaded = tomllib.load(f)
    assert loaded["station"]["callsign"]    == "K1JT"      # overwritten
    assert loaded["station"]["grid_square"] == "EM38ww40pk"  # preserved
    assert loaded["paths"]["keep_wav"]      is False         # preserved


# ---------- serializer ------------------------------------------------------

def test_serialize_toml_round_trips_via_tomllib() -> None:
    src = {
        "station":    {"callsign": "AC0G", "grid_square": "EM38ww40pk"},
        "paths":      {"keep_wav": False},
        "processing": {"radiod_lifetime_frames": 6000},
        "radiod": [
            {
                "id": "test-rx888",
                "radiod_status": "test-status.local",
                "msk144": {"sample_rate": 12000, "freqs_hz": [28130000, 50260000]},
            },
        ],
    }
    text = configurator._serialize_toml(src)
    loaded = tomllib.loads(text)
    assert loaded == src


def test_serialize_toml_emits_array_of_tables() -> None:
    """[[radiod]] blocks must render with the right header syntax."""
    text = configurator._serialize_toml({
        "radiod": [
            {"id": "a", "radiod_status": "a.local"},
            {"id": "b", "radiod_status": "b.local"},
        ],
    })
    assert text.count("[[radiod]]") == 2
    loaded = tomllib.loads(text)
    assert [b["id"] for b in loaded["radiod"]] == ["a", "b"]


def test_serialize_toml_inline_arrays() -> None:
    """freqs_hz lists should render on one line."""
    text = configurator._serialize_toml({
        "radiod": [
            {"id": "x", "radiod_status": "x.local",
             "msk144": {"freqs_hz": [28130000, 50260000]}},
        ],
    })
    # One line with the whole array, not separate lines.
    assert any("[28130000, 50260000]" in line for line in text.splitlines())


# ---------- wizard availability --------------------------------------------

def test_wizard_available_false_without_tty() -> None:
    """In pytest stdout isn't a TTY; the dispatcher must NOT try to exec
    the wizard.  Otherwise piping `meteor-scatter config init` would hang."""
    assert configurator._wizard_available() is False


def test_wizard_available_false_when_script_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(configurator, "_WIZARD_PATH", tmp_path / "nope.sh")
    assert configurator._wizard_available() is False


# ---------- sigmond.wizard_dispatch delegation -----------------------------
# Same shape as mag-recorder's tests (mag-recorder commit 52190e7).  Once
# the third client (wspr-recorder) also adopts the lib, the test bodies
# will be near-identical across three repos -- a hint the test helpers
# might be the next thing to extract into a shared sigmond test-support
# module.

def test_wizard_dispatch_delegates_to_sigmond_when_available(monkeypatch) -> None:
    """When sigmond.wizard_dispatch is importable, _wizard_available
    must defer to sigmond's is_wizard_available(args, _WIZARD_PATH)."""
    captured = {}

    class _FakeWD:
        SIGMOND_WIZARD_DISPATCH_API = "1"
        @staticmethod
        def is_wizard_available(args, wizard_path):
            captured["args"]         = args
            captured["wizard_path"]  = wizard_path
            return True
    monkeypatch.setattr(configurator, "_sigmond_wd", _FakeWD)

    args = argparse.Namespace(non_interactive=False)
    assert configurator._wizard_available(args) is True
    assert captured["args"]        is args
    assert captured["wizard_path"] == configurator._WIZARD_PATH


def test_wizard_dispatch_falls_back_when_sigmond_absent(monkeypatch, tmp_path) -> None:
    """With sigmond.wizard_dispatch unavailable, _wizard_available
    must use the local TTY+whiptail+script-exists check."""
    monkeypatch.setattr(configurator, "_sigmond_wd", None)
    monkeypatch.setattr(configurator, "_WIZARD_PATH", tmp_path / "absent.sh")
    assert configurator._wizard_available(argparse.Namespace(non_interactive=False)) is False


def test_exec_wizard_threads_env_through_sigmond(monkeypatch, tmp_path) -> None:
    """Pins the env-var contract (METEOR_SCATTER_HELP_TOML,
    METEOR_SCATTER_CLI, SIGMOND_WIZARD_LIB_SH) + extra_args shape +
    parse=None semantics for meteor-scatter."""
    captured = {}
    fake_lib_sh = tmp_path / "wizard_dispatch.sh"
    fake_lib_sh.write_text("# fake\n")

    class _FakeResult:
        returncode = 0
        error      = None

    class _FakeWD:
        SIGMOND_WIZARD_DISPATCH_API = "1"
        @staticmethod
        def exec_wizard(wizard_path, *, extra_env=None, parse=None, extra_args=None):
            captured["wizard_path"]  = wizard_path
            captured["extra_env"]    = extra_env
            captured["parse"]        = parse
            captured["extra_args"]   = extra_args
            return _FakeResult()
    monkeypatch.setattr(configurator, "_sigmond_wd",            _FakeWD)
    monkeypatch.setattr(configurator, "_SIGMOND_WIZARD_LIB_SH", fake_lib_sh)

    args = argparse.Namespace(non_interactive=False,
                              config=Path("/etc/meteor-scatter/meteor-scatter-config.toml"))
    rc = configurator._exec_wizard(args, "edit")
    assert rc == 0
    assert captured["extra_args"] == [
        "edit", "--config", "/etc/meteor-scatter/meteor-scatter-config.toml",
    ]
    # parse=None: meteor-scatter's wizard pipes JSON to `config apply` itself
    assert captured["parse"] is None
    env = captured["extra_env"]
    assert "METEOR_SCATTER_HELP_TOML" in env
    assert "METEOR_SCATTER_CLI"       in env
    assert env["SIGMOND_WIZARD_LIB_SH"] == str(fake_lib_sh)


def test_exec_wizard_falls_back_to_legacy_when_sigmond_absent(monkeypatch) -> None:
    """When sigmond isn't installed, _exec_wizard uses the
    pre-extraction local subprocess.call path."""
    captured = {}
    monkeypatch.setattr(configurator, "_sigmond_wd", None)
    monkeypatch.setattr(configurator, "_SIGMOND_WIZARD_LIB_SH", None)

    def _fake_call(cmd, env=None):
        captured["cmd"] = cmd
        captured["env"] = env
        return 7
    monkeypatch.setattr(configurator.subprocess, "call", _fake_call)

    args = argparse.Namespace(non_interactive=False, config=None)
    rc = configurator._exec_wizard(args, "init")
    assert rc == 7
    assert captured["cmd"][0] == str(configurator._WIZARD_PATH)
    assert captured["cmd"][1] == "init"
    assert captured["env"]["METEOR_SCATTER_HELP_TOML"] == str(configurator._HELP_TOML_PATH)


def test_exec_wizard_surfaces_sigmond_error(monkeypatch) -> None:
    """When sigmond's exec_wizard returns .error, _exec_wizard logs
    it and returns 1 -- not bubble the error up."""
    class _FakeResult:
        returncode = 0
        error      = "exec failed: [Errno 2] No such file"

    class _FakeWD:
        SIGMOND_WIZARD_DISPATCH_API = "1"
        @staticmethod
        def exec_wizard(*a, **kw):
            return _FakeResult()
    monkeypatch.setattr(configurator, "_sigmond_wd", _FakeWD)

    args = argparse.Namespace(non_interactive=False, config=None)
    rc = configurator._exec_wizard(args, "init")
    assert rc == 1


# ---------- [timing] apply -------------------------------------------------

def test_apply_writes_timing_section(tmp_path: Path) -> None:
    rv = _apply({"timing": {"chain_delay_ns": 12345}},
                tmp_path, existing=_FIXTURE_WITH_RADIOD)
    assert rv == 0
    with open(tmp_path / "c.toml", "rb") as f:
        loaded = tomllib.load(f)
    assert loaded["timing"]["chain_delay_ns"] == 12345


# ---------- [[radiod]] apply (overlay-wins) --------------------------------

def test_apply_writes_radiod_blocks(tmp_path: Path) -> None:
    """The operator's full block list replaces the file's list."""
    rv = _apply({"radiod": [
                    {"status": "new-status.local"},
                    {"status": "new2-status.local"},
                ]}, tmp_path, existing=_FIXTURE_WITH_RADIOD)
    assert rv == 0
    with open(tmp_path / "c.toml", "rb") as f:
        loaded = tomllib.load(f)
    statuses = [b["status"] for b in loaded["radiod"]]
    assert statuses == ["new-status.local", "new2-status.local"]
    # The original 'test-rx888' block (and its freqs_hz) is GONE because
    # overlay-wins replaces the whole list.  This is the documented
    # contract; operators who want to preserve freqs_hz pass them
    # back in the payload, or use the wizard's "Edit raw TOML" path.
    assert "test-status.local" not in statuses


def test_apply_rejects_radiod_missing_id(tmp_path: Path, capsys) -> None:
    # legacy `radiod_status` alone does not satisfy apply, which requires `status`
    rv = _apply({"radiod": [{"radiod_status": "x.local"}]}, tmp_path)
    assert rv == 2
    assert "status is required" in capsys.readouterr().err.lower()


def test_apply_rejects_radiod_missing_status(tmp_path: Path, capsys) -> None:
    rv = _apply({"radiod": [{"id": "x"}]}, tmp_path)
    assert rv == 2
    assert "status is required" in capsys.readouterr().err.lower()


def test_apply_rejects_radiod_duplicate_status(tmp_path: Path, capsys) -> None:
    rv = _apply({"radiod": [
                    {"status": "dup.local"},
                    {"status": "dup.local"},
                ]}, tmp_path)
    assert rv == 2
    assert "duplicate status names" in capsys.readouterr().err.lower()


def test_apply_rejects_radiod_not_a_list(tmp_path: Path, capsys) -> None:
    rv = _apply({"radiod": {"id": "x"}}, tmp_path)  # dict, not list
    assert rv == 2
    assert "must be a list" in capsys.readouterr().err.lower()


# ---------- env show / env apply ------------------------------------------

def _env_ns(**kw) -> argparse.Namespace:
    base = {"instance": None, "json": True, "log_level": None, "path": "-",
            "config": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_env_show_missing_file_returns_empty(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(configurator, "_ENV_DIR", tmp_path)
    rv = configurator.cmd_env_show(_env_ns(instance="nope-rx888"))
    assert rv == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_env_show_parses_existing_file(tmp_path: Path, monkeypatch, capsys) -> None:
    # `env show` parses whatever KEY=VALUE pairs are in the file verbatim
    # (it does not filter on the managed-key set), so a hand-added knob
    # still shows through.
    monkeypatch.setattr(configurator, "_ENV_DIR", tmp_path)
    (tmp_path / "rx0.env").write_text(
        '# leading comment\n'
        'SOME_OPERATOR_KEY=1\n'
        'ANOTHER_KEY="a b"\n'
        '\n'
    )
    rv = configurator.cmd_env_show(_env_ns(instance="rx0"))
    assert rv == 0
    out = json.loads(capsys.readouterr().out)
    assert out["SOME_OPERATOR_KEY"] == "1"
    assert out["ANOTHER_KEY"]       == "a b"


def test_env_show_requires_instance(monkeypatch, capsys) -> None:
    rv = configurator.cmd_env_show(_env_ns(instance=None))
    assert rv == 2
    assert "--instance" in capsys.readouterr().err.lower()


def _env_apply(payload, tmp_path: Path, monkeypatch, *,
               instance: str = "rx0", existing: str = "") -> int:
    monkeypatch.setattr(configurator, "_ENV_DIR", tmp_path)
    if existing:
        (tmp_path / f"{instance}.env").write_text(existing)
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        return configurator.cmd_env_apply(_env_ns(instance=instance))
    finally:
        sys.stdin = old_stdin


# The deposit-only build manages NO env keys (the PSKReporter-era
# delivery knobs were removed with the uploader; the wsprdaemon.org
# upload knobs arrive in Phase 3).  `env apply` therefore rejects any
# payload key, so a stale knob can't silently do nothing.

def test_env_apply_rejects_any_key(tmp_path: Path, monkeypatch, capsys) -> None:
    rv = _env_apply({"METEOR_SCATTER_ANYTHING": "value"}, tmp_path, monkeypatch)
    assert rv == 2
    assert "unknown / unmanaged" in capsys.readouterr().err.lower()


def test_env_apply_empty_payload_is_accepted(tmp_path: Path, monkeypatch) -> None:
    # An empty payload manages nothing and is a no-op success — the
    # writable-key set is empty, so there is nothing to reject.
    rv = _env_apply({}, tmp_path, monkeypatch)
    assert rv == 0


# ---------- environment-cache radiod picker (cross-pollinated from wspr-recorder)

# These pin the parser logic that lives inside scripts/config-wizard.sh's
# pick_radiod_status function.  The shell scaffolding around it (menu
# construction, fallback path) is mechanical -- the interesting bit is
# this filter, and we want a regression test that catches the next time
# sigmond changes the cache schema.

import subprocess


def _run_parser(cache_path: Path) -> list[tuple[str, str]]:
    """Run the same Python heredoc the wizard runs, return parsed
    (endpoint, label) tuples."""
    src = '''
import json, os
try:
    data = json.load(open(os.environ['CACHE']))
except Exception:
    raise SystemExit(0)
seen = set()
for obs in data.get('observations') or []:
    if obs.get('source') not in ('mdns', 'multicast'):
        continue
    if obs.get('kind') != 'radiod' or not obs.get('ok', True):
        continue
    endpoint = (obs.get('endpoint') or '').rsplit(':', 1)[0]
    if not endpoint or endpoint in seen:
        continue
    seen.add(endpoint)
    fields = obs.get('fields') or {}
    label = (fields.get('mdns_name') or obs.get('id') or endpoint).strip()
    print(f'{endpoint}|{label}')
'''
    out = subprocess.run(
        ["python3", "-c", src],
        env={"CACHE": str(cache_path)},
        capture_output=True, text=True, check=False,
    ).stdout
    return [tuple(line.split("|", 1)) for line in out.splitlines() if line]


def test_env_cache_parser_handles_multicast_source(tmp_path: Path) -> None:
    """bee1's cache uses source='multicast' (not 'mdns').  wspr-recorder's
    original port only accepted 'mdns', which yielded an empty cache on
    multicast-discovery hosts.  Our port must accept both."""
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": [{
        "source": "multicast", "kind": "radiod", "ok": True,
        "endpoint": "bee1-status.local", "id": "bee1-rx888",
        "fields": {},
    }]}))
    out = _run_parser(cache)
    assert out == [("bee1-status.local", "bee1-rx888")]


def test_env_cache_parser_handles_mdns_source(tmp_path: Path) -> None:
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": [{
        "source": "mdns", "kind": "radiod", "ok": True,
        "endpoint": "ax.local", "id": "ax-rx888",
        "fields": {"mdns_name": "AC0G @EM38ww B1 T3FD"},
    }]}))
    out = _run_parser(cache)
    assert out == [("ax.local", "AC0G @EM38ww B1 T3FD")]


def test_env_cache_parser_strips_port_from_endpoint(tmp_path: Path) -> None:
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": [{
        "source": "mdns", "kind": "radiod", "ok": True,
        "endpoint": "h.local:5006", "fields": {},
    }]}))
    out = _run_parser(cache)
    assert out == [("h.local", "h.local")]


def test_env_cache_parser_skips_non_radiod_kinds(tmp_path: Path) -> None:
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": [
        {"source": "mdns", "kind": "gpsdo", "ok": True, "endpoint": "g.local"},
        {"source": "ntp",  "kind": "time_source", "ok": True, "endpoint": "n:123"},
        {"source": "mdns", "kind": "radiod", "ok": True, "endpoint": "r.local"},
    ]}))
    out = _run_parser(cache)
    assert out == [("r.local", "r.local")]


def test_env_cache_parser_skips_failed_observations(tmp_path: Path) -> None:
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": [
        {"source": "mdns", "kind": "radiod", "ok": False, "endpoint": "bad.local"},
        {"source": "mdns", "kind": "radiod", "ok": True,  "endpoint": "good.local"},
    ]}))
    out = _run_parser(cache)
    assert [endpoint for endpoint, _ in out] == ["good.local"]


def test_env_cache_parser_deduplicates_repeated_endpoints(tmp_path: Path) -> None:
    """If sigmond's discovery wrote two observations for the same radiod
    (e.g. both mdns and multicast saw bee1), the picker should show one
    menu row, not two."""
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": [
        {"source": "mdns",      "kind": "radiod", "ok": True, "endpoint": "bee1.local"},
        {"source": "multicast", "kind": "radiod", "ok": True, "endpoint": "bee1.local"},
    ]}))
    out = _run_parser(cache)
    assert len(out) == 1


def test_env_cache_parser_returns_empty_on_missing_or_invalid(tmp_path: Path) -> None:
    assert _run_parser(tmp_path / "absent.json")        == []
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    assert _run_parser(bad)                              == []


def test_env_cache_parser_returns_empty_on_no_observations(tmp_path: Path) -> None:
    cache = tmp_path / "env-cache.json"
    cache.write_text(json.dumps({"observations": []}))
    assert _run_parser(cache) == []


# ---------- env-file serializer / parser ----------------------------------

def test_serialize_env_file_quotes_values_with_whitespace() -> None:
    text = configurator._serialize_env_file({"K": "value with spaces"})
    assert text == 'K="value with spaces"\n'


def test_serialize_env_file_quotes_values_with_equals() -> None:
    text = configurator._serialize_env_file({"K": "a=b"})
    assert text == 'K="a=b"\n'


def test_parse_env_file_strips_quotes() -> None:
    f = Path("/tmp/psk-env-parse-test.env")
    f.write_text('K1="quoted"\nK2=\'single\'\nK3=plain\n')
    try:
        parsed = configurator._parse_env_file(f)
        assert parsed == {"K1": "quoted", "K2": "single", "K3": "plain"}
    finally:
        f.unlink()


def test_parse_env_file_skips_comments_and_blanks() -> None:
    f = Path("/tmp/psk-env-parse-test2.env")
    f.write_text('# this is a comment\n\n\nKEY=value\n# trailing comment\n')
    try:
        parsed = configurator._parse_env_file(f)
        assert parsed == {"KEY": "value"}
    finally:
        f.unlink()
