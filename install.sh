#!/usr/bin/env bash
# ============================================================================
#  Downloader Bot — automated installer
#
#  Supported systems (Windows excluded):
#    Debian/Ubuntu/Mint, Fedora/RHEL/Rocky/Alma, openSUSE, Arch/Manjaro,
#    Alpine, Void, Gentoo, FreeBSD, macOS
#
#  What it does:
#    1. Auto-detects the OS and package manager
#    2. Installs Python 3.10+, ffmpeg, git and venv tools
#    3. Creates a virtual environment (venv) and installs requirements.txt
#    4. Interactively asks for the bot token, admin id, bot name, etc.
#    5. Optional: start menu links, Local Bot API, autostart
#
#  Usage:  bash install.sh
# ============================================================================

set -u

# ── Colors / helpers ─────────────────────────────────────────────────────────
if [ -t 1 ]; then
    C_RESET='\033[0m'; C_OK='\033[0;32m'; C_WARN='\033[0;33m'
    C_ERR='\033[0;31m'; C_INFO='\033[0;36m'; C_BOLD='\033[1m'
else
    C_RESET=''; C_OK=''; C_WARN=''; C_ERR=''; C_INFO=''; C_BOLD=''
fi
info()  { printf "${C_INFO}==>${C_RESET} %s\n" "$*"; }
ok()    { printf "${C_OK}[OK]${C_RESET} %s\n" "$*"; }
warn()  { printf "${C_WARN}[!]${C_RESET} %s\n" "$*"; }
err()   { printf "${C_ERR}[ERROR]${C_RESET} %s\n" "$*" >&2; }
die()   { err "$*"; exit 1; }
ask()   { # ask "Question" "default" -> writes the answer to stdout
    local prompt="$1" default="${2:-}" reply
    if [ -n "$default" ]; then
        printf "%s [%s]: " "$prompt" "$default" > /dev/tty
    else
        printf "%s: " "$prompt" > /dev/tty
    fi
    read -r reply < /dev/tty || reply=""
    printf '%s' "${reply:-$default}"
}
ask_yn() { # ask_yn "Question" "y|n" -> 0=yes 1=no
    local prompt="$1" default="${2:-n}" reply
    local hint="[y/N]"; [ "$default" = "y" ] && hint="[Y/n]"
    printf "%s %s: " "$prompt" "$hint" > /dev/tty
    read -r reply < /dev/tty || reply=""
    reply="${reply:-$default}"
    case "$reply" in
        y|Y|yes|Yes) return 0 ;;
        *) return 1 ;;
    esac
}

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
ENV_FILE="$PROJECT_DIR/data/.env"
RUN_USER="${SUDO_USER:-$(id -un)}"

cd "$PROJECT_DIR" || die "Could not enter the project directory: $PROJECT_DIR"

printf "${C_BOLD}\n  Downloader Bot Setup\n  Project: %s\n  User: %s\n${C_RESET}\n" "$PROJECT_DIR" "$RUN_USER"

# ── Is sudo needed ───────────────────────────────────────────────────────────
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        warn "You're not root and sudo isn't available. Package installation may fail."
    fi
fi

# ── 1. OS / package manager detection ────────────────────────────────────────
OS="$(uname -s)"
PKG=""
INIT="none"

detect_system() {
    case "$OS" in
        Linux)
            if [ -r /etc/os-release ]; then
                . /etc/os-release
            fi
            for pm in apt-get dnf zypper pacman apk xbps-install emerge; do
                if command -v "$pm" >/dev/null 2>&1; then PKG="$pm"; break; fi
            done
            if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
                INIT="systemd"
            elif command -v rc-update >/dev/null 2>&1; then
                INIT="openrc"
            fi
            ;;
        Darwin)
            PKG="brew"; INIT="launchd"
            ;;
        FreeBSD)
            PKG="pkg"; INIT="rcd"
            ;;
        *)
            warn "Unknown operating system: $OS — will still try with Python."
            ;;
    esac
}
detect_system

info "Operating system : $OS ${PRETTY_NAME:+($PRETTY_NAME)}"
info "Package manager  : ${PKG:-not found}"
info "Init system      : $INIT"
echo

# ── 2. Install system packages ───────────────────────────────────────────────
install_packages() {
    info "Installing required system packages (python3, ffmpeg, git)..."
    case "$PKG" in
        apt-get)
            $SUDO apt-get update -y
            $SUDO apt-get install -y python3 python3-venv python3-pip ffmpeg git
            ;;
        dnf)
            $SUDO dnf install -y python3 python3-pip git
            if ! $SUDO dnf install -y ffmpeg; then
                warn "ffmpeg isn't in the default repos. RHEL/Rocky/Alma may need RPM Fusion:"
                warn "  sudo dnf install -y https://download1.rpmfusion.org/free/el/rpmfusion-free-release-\$(rpm -E %rhel).noarch.rpm"
                warn "  On Fedora: sudo dnf install -y ffmpeg --allowerasing"
            fi
            ;;
        zypper)
            $SUDO zypper --non-interactive install python3 python3-pip git ffmpeg \
                || warn "ffmpeg may require the Packman repo on openSUSE."
            ;;
        pacman)
            $SUDO pacman -Sy --noconfirm python python-pip ffmpeg git
            ;;
        apk)
            $SUDO apk add python3 py3-pip ffmpeg git
            ;;
        xbps-install)
            $SUDO xbps-install -Sy python3 python3-pip ffmpeg git
            ;;
        emerge)
            $SUDO emerge --noreplace dev-lang/python media-video/ffmpeg dev-vcs/git
            ;;
        pkg)
            $SUDO pkg install -y python3 ffmpeg git
            ;;
        brew)
            brew install python ffmpeg git
            ;;
        *)
            warn "No package manager found; you may need to install python3/ffmpeg/git manually."
            ;;
    esac
}
install_packages

# ── 3. Python version check ──────────────────────────────────────────────────
PYTHON=""
for c in python3 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$c" >/dev/null 2>&1; then
        if "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
            PYTHON="$c"; break
        fi
    fi
done
[ -n "$PYTHON" ] || die "Python 3.10+ not found. Please install it manually."
ok "Found Python: $($PYTHON --version 2>&1)"

command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg is ready." || warn "ffmpeg not found — audio downloads may not work."

# ── 4. venv + dependencies ───────────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    info "Creating the virtual environment: $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR" || die "Could not create the venv (is python3-venv installed?)."
else
    info "Using the existing virtual environment: $VENV_DIR"
fi

VPY="$VENV_DIR/bin/python"
info "Upgrading pip and installing requirements.txt..."
"$VPY" -m pip install --upgrade pip >/dev/null 2>&1 || warn "Could not upgrade pip, continuing."
"$VPY" -m pip install -r "$PROJECT_DIR/requirements.txt" || die "Could not install dependencies."
ok "Dependencies installed (yt-dlp, gallery-dl, python-telegram-bot...)."

mkdir -p "$PROJECT_DIR/data/downloads" "$PROJECT_DIR/data/logs"

# ── 5. Interactive configuration (data/.env) ─────────────────────────────────
echo
printf "${C_BOLD}── Configuration ─────────────────────────────────────────────${C_RESET}\n"

WRITE_ENV="y"
if [ -f "$ENV_FILE" ]; then
    if ! ask_yn "data/.env already exists. Reconfigure it?" "n"; then
        WRITE_ENV="n"
        info "Keeping the existing data/.env."
    fi
fi

if [ "$WRITE_ENV" = "y" ]; then
    BOT_TOKEN=""
    while [ -z "$BOT_TOKEN" ]; do
        BOT_TOKEN="$(ask "Bot Token (@BotFather)")"
        [ -z "$BOT_TOKEN" ] && warn "The bot token is required."
    done

    ADMIN_ID=""
    while ! printf '%s' "$ADMIN_ID" | grep -Eq '^[0-9]+$'; do
        ADMIN_ID="$(ask "Admin Telegram ID (numeric, via @userinfobot)")"
        printf '%s' "$ADMIN_ID" | grep -Eq '^[0-9]+$' || warn "Enter a numeric ID."
    done

    BOT_NAME="$(ask "Bot name" "Downloader")"

    SHOW_LINKS="false"; OWNER_LINK=""; COMMUNITY_LINK=""; COMMUNITY_LABEL="Community"
    if ask_yn "Show owner/community links in the start menu?" "n"; then
        SHOW_LINKS="true"
        OWNER_LINK="$(ask "Owner link (e.g. https://t.me/username, can be left empty)")"
        COMMUNITY_LINK="$(ask "Community/channel link (can be left empty)")"
        if [ -n "$COMMUNITY_LINK" ]; then
            COMMUNITY_LABEL="$(ask "Community button label" "Community")"
        fi
    fi

    LOCAL_BOT_API_BASE=""; TELEGRAM_API_ID=""; TELEGRAM_API_HASH=""; MAX_FILE_SIZE_MB="1900"
    if ask_yn "Use a Local Bot API for files over 50 MB?" "n"; then
        LOCAL_BOT_API_BASE="$(ask "Local Bot API address" "http://127.0.0.1:8081")"
        TELEGRAM_API_ID="$(ask "TELEGRAM_API_ID (my.telegram.org)")"
        TELEGRAM_API_HASH="$(ask "TELEGRAM_API_HASH")"
        MAX_FILE_SIZE_MB="$(ask "Maximum file size (MB)" "1900")"
    else
        info "Local Bot API is off — the Telegram 50 MB limit applies."
    fi

    MAX_SIMULTANEOUS_DOWNLOADS="$(ask "Maximum concurrent downloads" "3")"

    info "Writing data/.env..."
    cat > "$ENV_FILE" <<EOF
# Downloader Bot configuration — generated by install.sh
BOT_NAME="$BOT_NAME"
BOT_USERNAME=""
BOT_TOKEN="$BOT_TOKEN"
ADMIN_ID="$ADMIN_ID"

SHOW_LINKS="$SHOW_LINKS"
OWNER_LINK="$OWNER_LINK"
COMMUNITY_LINK="$COMMUNITY_LINK"
COMMUNITY_LABEL="$COMMUNITY_LABEL"

MAX_SIMULTANEOUS_DOWNLOADS="$MAX_SIMULTANEOUS_DOWNLOADS"
MAX_FILE_SIZE_MB="$MAX_FILE_SIZE_MB"

LOCAL_BOT_API_BASE="$LOCAL_BOT_API_BASE"
TELEGRAM_API_ID="$TELEGRAM_API_ID"
TELEGRAM_API_HASH="$TELEGRAM_API_HASH"

COOKIES_FILE="data/cookies.txt"
DOWNLOAD_DIR="data/downloads"
LOG_DIR="data/logs"

CLEANUP_TZ_OFFSET="0"
CLEANUP_HOUR="0"
LOG_RETENTION_DAYS="7"
CACHE_ENABLED="true"
EOF
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    ok "Configuration saved: $ENV_FILE"
fi

# ── 6. Quick sanity check ────────────────────────────────────────────────────
info "Verifying the install (import test)..."
if "$VPY" -c "import bot.app" 2>/dev/null; then
    ok "Modules load fine."
else
    warn "The import test failed; check your dependencies."
fi

# ── 7. Autostart (optional) ──────────────────────────────────────────────────
SERVICE_NAME="downloader-bot"
echo
printf "${C_BOLD}── Autostart ─────────────────────────────────────────────────${C_RESET}\n"

setup_systemd() {
    local unit="/etc/systemd/system/${SERVICE_NAME}.service"
    info "Creating the systemd service: $unit"
    $SUDO tee "$unit" >/dev/null <<EOF
[Unit]
Description=${BOT_NAME:-Downloader} Telegram Bot
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=20

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$PROJECT_DIR
Environment="PATH=$VENV_DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=$VPY $PROJECT_DIR/start.py
Restart=always
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=30
KillMode=mixed
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now "${SERVICE_NAME}.service"
    ok "Service is active. Status: sudo systemctl status ${SERVICE_NAME}"
    info "Live log: journalctl -u ${SERVICE_NAME} -f"
}

setup_openrc() {
    local script="/etc/init.d/${SERVICE_NAME}"
    info "Creating the OpenRC service: $script"
    $SUDO tee "$script" >/dev/null <<EOF
#!/sbin/openrc-run
name="${SERVICE_NAME}"
description="${BOT_NAME:-Downloader} Telegram Bot"
command="$VPY"
command_args="$PROJECT_DIR/start.py"
command_user="$RUN_USER"
directory="$PROJECT_DIR"
pidfile="/run/\${RC_SVCNAME}.pid"
command_background="yes"
output_log="$PROJECT_DIR/data/logs/openrc.log"
error_log="$PROJECT_DIR/data/logs/openrc.log"

depend() {
    need net
}
EOF
    $SUDO chmod +x "$script"
    $SUDO rc-update add "${SERVICE_NAME}" default
    $SUDO rc-service "${SERVICE_NAME}" start
    ok "OpenRC service added and started."
}

setup_launchd() {
    local plist="$HOME/Library/LaunchAgents/com.${SERVICE_NAME}.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    info "Creating the launchd agent: $plist"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.${SERVICE_NAME}</string>
  <key>ProgramArguments</key>
  <array><string>$VPY</string><string>$PROJECT_DIR/start.py</string></array>
  <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PROJECT_DIR/data/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_DIR/data/logs/launchd.log</string>
</dict>
</plist>
EOF
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load "$plist"
    ok "launchd agent loaded (starts automatically at login)."
}

setup_rcd() {
    local script="/usr/local/etc/rc.d/${SERVICE_NAME}"
    info "Creating the FreeBSD rc.d service: $script"
    $SUDO tee "$script" >/dev/null <<EOF
#!/bin/sh
# PROVIDE: ${SERVICE_NAME}
# REQUIRE: NETWORKING
# KEYWORD: shutdown
. /etc/rc.subr
name="${SERVICE_NAME}"
rcvar="${SERVICE_NAME}_enable"
pidfile="/var/run/\${name}.pid"
command="/usr/sbin/daemon"
command_args="-p \${pidfile} $VPY $PROJECT_DIR/start.py"
load_rc_config \$name
run_rc_command "\$1"
EOF
    $SUDO chmod +x "$script"
    $SUDO sysrc "${SERVICE_NAME}_enable=YES"
    $SUDO service "${SERVICE_NAME}" start
    ok "rc.d service added and started."
}

if ask_yn "Start the bot automatically at boot?" "y"; then
    case "$INIT" in
        systemd) setup_systemd ;;
        openrc)  setup_openrc ;;
        launchd) setup_launchd ;;
        rcd)     setup_rcd ;;
        *)
            warn "No supported init system found for autostart."
            warn "You can start the bot manually:"
            printf "    %s %s\n" "$VPY" "$PROJECT_DIR/start.py"
            ;;
    esac
else
    info "Skipped autostart. To start manually:"
    printf "    %s %s\n" "$VPY" "$PROJECT_DIR/start.py"
fi

echo
ok "Setup complete."
