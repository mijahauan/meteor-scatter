#!/bin/bash
# install.sh — first-run bootstrap for meteor-scatter (Pattern A editable install)
#
# Usage: sudo ./scripts/install.sh [--pull] [--yes]
#
# What it does:
#   1. Creates service user meteorscat:meteorscat
#   2. Clones/links repo to /opt/git/sigmond/meteor-scatter
#   3. Creates venv at /opt/git/sigmond/meteor-scatter/venv with editable install
#   4. Verifies the bundled jt9 MSK144 decoder for this arch
#   5. Renders config template (non-destructive — never overwrites)
#   6. Installs systemd unit template
#   7. Enables meteor-scatter@<radiod_id> instances from config
#
# The MSK144 decoder (WSJT-X's jt9) is bundled in-repo at
# bin/decoders/jt9-{x86,arm32,arm64}-v* — no build step.  Upload to
# wsprdaemon.org is deferred (Phase 3): decoded spots accumulate in
# sigmond's local SQLite sink until the upload transport is wired.
#
# Idempotent: safe to re-run.

set -euo pipefail

SERVICE_USER="meteorscat"
SERVICE_GROUP="meteorscat"
REPO_SOURCE="/opt/git/sigmond/meteor-scatter"
VENV_DIR="/opt/git/sigmond/meteor-scatter/venv"
CONFIG_DIR="/etc/meteor-scatter"
CONFIG_FILE="${CONFIG_DIR}/meteor-scatter-config.toml"
SPOOL_DIR="/var/lib/meteor-scatter"
LOG_DIR="/var/log/meteor-scatter"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ui_info()  { echo "[INFO]  $*"; }
ui_warn()  { echo "[WARN]  $*" >&2; }
ui_error() { echo "[ERROR] $*" >&2; }

# --- Phase 0: arg parsing ---
DO_PULL=false
AUTO_YES=false
for arg in "$@"; do
    case "$arg" in
        --pull) DO_PULL=true ;;
        --yes)  AUTO_YES=true ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    ui_error "Must run as root (sudo)"
    exit 1
fi

# --- Phase 1: service user ---
if ! id -u "$SERVICE_USER" &>/dev/null; then
    ui_info "Creating service user $SERVICE_USER"
    useradd --system --shell /usr/sbin/nologin \
            --home-dir /nonexistent --no-create-home \
            "$SERVICE_USER"
fi

# Add SERVICE_USER to the sigmond supplementary group so meteor-scatter can
# write to /var/lib/hs-uploader/watermarks.db and /var/lib/sigmond/sink.db.
# (The sigmond group is created by sigmond/install.sh, and the shared
# /var/lib/hs-uploader dir is provisioned by hs-uploader/install.sh's
# tmpfiles.d entry.  If neither has run yet, skip — re-run after they have.)
if getent group sigmond &>/dev/null; then
    if ! id -nG "$SERVICE_USER" 2>/dev/null | tr ' ' '\n' | grep -qx sigmond; then
        usermod -a -G sigmond "$SERVICE_USER"
        ui_info "Added $SERVICE_USER to sigmond group"
    fi
else
    ui_info "sigmond group not present yet — re-run after sigmond install"
fi

# --- Phase 1.4: ensure uv is on PATH (canonical sigmond-suite installer) ---
# Delegates to sigmond's shared helper if present; inline fallback for
# the bootstrap case.  Keep the fallback in sync with
# sigmond/scripts/install/ensure_uv.sh.
_ENSURE_UV_SH="/opt/git/sigmond/sigmond/scripts/install/ensure_uv.sh"
if [[ -r "$_ENSURE_UV_SH" ]]; then
    # shellcheck source=/dev/null
    source "$_ENSURE_UV_SH"
else
    _ensure_uv() {
        if command -v uv >/dev/null 2>&1; then
            printf '[INFO]  uv %s at %s\n' "$(uv --version 2>/dev/null | awk '{print $2}')" "$(command -v uv)"
            return 0
        fi
        printf '[INFO]  uv not found -- installing system-wide to /usr/local/bin\n'
        command -v curl >/dev/null || { printf '[ERROR] curl not found (apt install curl)\n' >&2; return 1; }
        if ! curl -LsSf https://astral.sh/uv/install.sh | env XDG_BIN_HOME=/usr/local/bin UV_NO_MODIFY_PATH=1 sh; then
            printf '[ERROR] uv installer failed\n' >&2
            return 1
        fi
        command -v uv >/dev/null || { printf '[ERROR] uv installer ran but uv is still not on PATH\n' >&2; return 1; }
        printf '[INFO]  uv %s installed\n' "$(uv --version 2>/dev/null | awk '{print $2}')"
    }
fi
_ensure_uv || { ui_error "_ensure_uv failed"; exit 1; }

# --- Phase 1.5: ensure sibling repos (callhash, hs-uploader, ka9q-python) ---
# pyproject.toml declares these via [tool.uv.sources] with `editable = true`
# and `path = "../<name>"`.  uv sync honors that natively (unlike plain
# pip), so we don't need an explicit pre-install pass anymore -- just
# verify the sibling repos exist on disk first.  If a sibling isn't at
# the canonical location, relocate from common alternates (~, ~/git,
# /opt/git) or clone upstream.
_ensure_sibling() {
    local name="$1" repo_url="$2"
    local target="/opt/git/sigmond/$name"

    if [[ -f "$target/pyproject.toml" ]]; then
        return 0
    fi

    ui_info "Sibling $name not at $target — searching common locations"
    local invoker="${SUDO_USER:-${USER:-$(id -un)}}"
    local src=""
    for candidate in \
        "/home/$invoker/$name" \
        "/home/$invoker/git/$name" \
        "/opt/git/$name"; do
        if [[ -f "$candidate/pyproject.toml" ]]; then
            src="$candidate"
            break
        fi
    done

    if [[ -n "$src" ]]; then
        ui_info "Found at $src — relocating to $target"
        if [[ -d "$target" && -n "$(ls -A "$target" 2>/dev/null)" ]]; then
            ui_error "$target exists and is non-empty — inspect and remove first"
            exit 1
        fi
        mkdir -p "$(dirname "$target")"
        [[ -d "$target" ]] && rmdir "$target"
        mv "$src" "$target"
        ui_info "Relocated $name to $target"
    else
        ui_info "Not found locally — cloning from $repo_url"
        git clone "$repo_url" "$target" || {
            ui_error "Failed to clone $repo_url"
            exit 1
        }
    fi
}

_ensure_sibling callhash    https://github.com/HamSCI/callhash
_ensure_sibling hs-uploader https://github.com/HamSCI/hs-uploader
_ensure_sibling ka9q-python https://github.com/HamSCI/ka9q-python

# --- Phase 2: repo + venv ---
if [[ ! -d "$REPO_SOURCE" ]]; then
    ui_info "Linking $REPO_ROOT -> $REPO_SOURCE"
    mkdir -p "$(dirname "$REPO_SOURCE")"
    ln -sfn "$REPO_ROOT" "$REPO_SOURCE"
fi

# Traversability check (Pattern A defense)
if ! sudo -u "$SERVICE_USER" test -r "$REPO_SOURCE/src/meteor_scatter/__init__.py"; then
    ui_error "Service user $SERVICE_USER cannot read $REPO_SOURCE/src/meteor_scatter/__init__.py"
    ui_error "Fix: ensure the repo is at /opt/git/sigmond/meteor-scatter (not under a mode-700 home)"
    ui_error "  or: chmod g+rx the path and add $SERVICE_USER to the owner's group"
    exit 1
fi

if $DO_PULL; then
    ui_info "Pulling latest from origin"
    git -C "$REPO_SOURCE" pull --ff-only
fi

# Recreate the venv if it doesn't exist.  (An incomplete venv from a
# crashed previous install is also handled here -- uv venv --allow-existing
# would normally fail loudly; rm+recreate is safer for the bootstrap case.)
if [[ ! -d "$VENV_DIR" ]]; then
    ui_info "Creating venv at $VENV_DIR"
    mkdir -p "$(dirname "$VENV_DIR")"
    # --seed populates pip/setuptools/wheel so the venv layout stays
    # consistent with pip-based tooling (e.g. the sigmond editable
    # install below).
    uv venv "$VENV_DIR" --python 3.11 --seed --quiet
fi

# uv sync reads pyproject.toml + uv.lock, resolves [tool.uv.sources]
# (callhash, hs-uploader, ka9q-python all editable from sibling paths),
# installs meteor-scatter itself editable into the venv, and pins exactly
# what's in uv.lock.  --no-dev skips dev extras (pytest etc.); --frozen
# requires uv.lock to be current (regenerate locally with `uv lock` if
# siblings or deps have shifted).
ui_info "Syncing meteor-scatter + siblings (callhash, hs-uploader, ka9q-python) into $VENV_DIR"
UV_PROJECT_ENVIRONMENT="$VENV_DIR" \
    uv sync --project "$REPO_SOURCE" --frozen --no-dev --quiet

# sigmond is the host-wide orchestrator; meteor-scatter lazy-imports
# sigmond.wizard_dispatch from configurator.py for the whiptail wizard
# plumbing (helpers shared with mag-recorder / wspr-recorder via
# sigmond's lib).  Falls back to a local implementation when absent
# so this install is recommended but not strictly required.  NOT
# declared in pyproject.toml so uv sync doesn't install it; explicit
# uv pip install when the sibling exists.
if [[ -d /opt/git/sigmond/sigmond ]]; then
    ui_info "Installing sigmond (editable) into venv"
    # uv pip install needs --python (not UV_PROJECT_ENVIRONMENT, which only
    # applies to project-level commands like uv sync).
    uv pip install --quiet --python "$VENV_DIR/bin/python3" -e /opt/git/sigmond/sigmond
else
    ui_info "sigmond repo not found at /opt/git/sigmond/sigmond -- wizard"
    ui_info "will use the local legacy-fallback dispatch."
fi

# Post-install verify: the daemon imports as the service user.
if ! sudo -u "$SERVICE_USER" "$VENV_DIR/bin/python3" -c 'import meteor_scatter' 2>/dev/null; then
    ui_error "Post-install verify failed: $SERVICE_USER cannot import meteor_scatter"
    exit 1
fi
ui_info "Post-install verify OK"

# --- Phase 2.5: verify the bundled jt9 MSK144 decoder ---
# meteor-scatter decodes with WSJT-X's jt9 (`jt9 --msk144`), bundled
# in-repo at bin/decoders/jt9-{x86,arm32,arm64}-v*.  No build step — just
# confirm the arch-specific binary is present, executable, and advertises
# the MSK144 mode.  Non-fatal: the recorder still records slots without a
# working decoder; it just can't decode until the binary resolves (the
# runtime falls back to a PATH `jt9` if the bundle is missing).
_verify_jt9() {
    local arch name jt9
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64)  name="jt9-x86-v27" ;;
        aarch64|arm64) name="jt9-arm64-v27" ;;
        armv7l|armv6l) name="jt9-arm32-v26" ;;
        *) ui_warn "no bundled jt9 for arch $arch — decoder falls back to PATH jt9"; return 0 ;;
    esac
    jt9="$REPO_SOURCE/bin/decoders/$name"
    if [[ ! -x "$jt9" ]]; then
        ui_warn "bundled jt9 $jt9 missing or not executable — recorder will record but NOT decode until resolved"
        return 0
    fi
    if "$jt9" --help 2>&1 | grep -qi -- '--msk144'; then
        ui_info "jt9 MSK144 decoder OK ($name)"
    else
        ui_warn "$jt9 does not advertise --msk144 — unexpected binary, decode may fail"
    fi
}
_verify_jt9

# --- Phase 3: config ---
mkdir -p "$CONFIG_DIR"
# The config is created by `meteor-scatter config init` (or `smd config init
# meteor-scatter`), which fills callsign/grid/radiod from the sigmond CONTRACT
# §14 env bag.  The installer no longer pre-renders a placeholder here: a
# placeholder made `config init` refuse ("already exists, pass --reconfig")
# and made Phase 7 enable a phantom @default instance whose radiod id never
# matched the one `config init` later wrote.
if [[ -f "$CONFIG_FILE" ]]; then
    ui_info "Config exists at $CONFIG_FILE — leaving it untouched"
else
    ui_info "No config yet — run: smd config init meteor-scatter  (or: meteor-scatter config init)"
fi

# --- Phase 4: directories ---
for dir in "$SPOOL_DIR" "$LOG_DIR"; do
    mkdir -p "$dir"
    chown "$SERVICE_USER:$SERVICE_GROUP" "$dir"
done

# --- Phase 5: systemd ---
ui_info "Installing systemd unit template"
install -o root -g root -m 644 \
    "$REPO_SOURCE/systemd/meteor-scatter@.service" \
    /etc/systemd/system/meteor-scatter@.service
systemctl daemon-reload

# --- Phase 6: (none) ---
# Unlike psk/wspr-recorder, meteor-scatter does NOT replace any native
# ka9q-radio decoder service: MSK144 monitors its own dial frequencies
# (28.130 / 50.260 MHz) and does not overlap the FT8/FT4 services those
# siblings own.  Nothing to disable here.

# --- Phase 7: instances ---
# Instance enablement follows configuration, not installation.  `config init`
# enables meteor-scatter@<radiod-id> for the id(s) it writes, so sigmond's
# lifecycle discovers and starts the right instance.  There is nothing to
# enable here until at least one radiod has been configured.

ui_info "Install complete. Configure + start with:"
ui_info "  smd config init meteor-scatter   # or: sudo meteor-scatter config init"
ui_info "  smd start --components meteor-scatter"
