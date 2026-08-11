#!/usr/bin/env bash
# ============================================================================
#  Downloader Bot — otomatik kurulum scripti
#
#  Desteklenen sistemler (Windows hariç):
#    Debian/Ubuntu/Mint, Fedora/RHEL/Rocky/Alma, openSUSE, Arch/Manjaro,
#    Alpine, Void, Gentoo, FreeBSD, macOS
#
#  Yaptıkları:
#    1. İşletim sistemini ve paket yöneticisini otomatik keşfeder
#    2. Python 3.10+, ffmpeg, git ve venv araçlarını kurar
#    3. Sanal ortam (venv) oluşturup requirements.txt yükler
#    4. İnteraktif olarak bot token, admin id, bot adı vb. sorar
#    5. İsteğe bağlı: start menüsü linkleri, Local Bot API, otomatik başlatma
#
#  Kullanım:  bash install.sh
# ============================================================================

set -u

# ── Renkler / yardımcılar ────────────────────────────────────────────────────
if [ -t 1 ]; then
    C_RESET='\033[0m'; C_OK='\033[0;32m'; C_WARN='\033[0;33m'
    C_ERR='\033[0;31m'; C_INFO='\033[0;36m'; C_BOLD='\033[1m'
else
    C_RESET=''; C_OK=''; C_WARN=''; C_ERR=''; C_INFO=''; C_BOLD=''
fi
info()  { printf "${C_INFO}==>${C_RESET} %s\n" "$*"; }
ok()    { printf "${C_OK}[OK]${C_RESET} %s\n" "$*"; }
warn()  { printf "${C_WARN}[!]${C_RESET} %s\n" "$*"; }
err()   { printf "${C_ERR}[HATA]${C_RESET} %s\n" "$*" >&2; }
die()   { err "$*"; exit 1; }
ask()   { # ask "Soru" "varsayilan" -> cevabı stdout'a yazar
    local prompt="$1" default="${2:-}" reply
    if [ -n "$default" ]; then
        printf "%s [%s]: " "$prompt" "$default" > /dev/tty
    else
        printf "%s: " "$prompt" > /dev/tty
    fi
    read -r reply < /dev/tty || reply=""
    printf '%s' "${reply:-$default}"
}
ask_yn() { # ask_yn "Soru" "y|n" -> 0=evet 1=hayır
    local prompt="$1" default="${2:-n}" reply
    local hint="[e/H]"; [ "$default" = "y" ] && hint="[E/h]"
    printf "%s %s: " "$prompt" "$hint" > /dev/tty
    read -r reply < /dev/tty || reply=""
    reply="${reply:-$default}"
    case "$reply" in
        e|E|y|Y|evet|Evet|yes|Yes) return 0 ;;
        *) return 1 ;;
    esac
}

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
ENV_FILE="$PROJECT_DIR/data/.env"
RUN_USER="${SUDO_USER:-$(id -un)}"

cd "$PROJECT_DIR" || die "Proje dizinine girilemedi: $PROJECT_DIR"

printf "${C_BOLD}\n  Downloader Bot Kurulumu\n  Proje: %s\n  Kullanıcı: %s\n${C_RESET}\n" "$PROJECT_DIR" "$RUN_USER"

# ── sudo gerekiyor mu ────────────────────────────────────────────────────────
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        warn "root değilsiniz ve sudo yok. Paket kurulumu başarısız olabilir."
    fi
fi

# ── 1. İşletim sistemi / paket yöneticisi keşfi ──────────────────────────────
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
            # init sistemi
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
            warn "Bilinmeyen işletim sistemi: $OS — yine de Python ile denenecek."
            ;;
    esac
}
detect_system

info "İşletim sistemi : $OS ${PRETTY_NAME:+($PRETTY_NAME)}"
info "Paket yöneticisi: ${PKG:-bulunamadı}"
info "Init sistemi    : $INIT"
echo

# ── 2. Sistem paketlerini kur ────────────────────────────────────────────────
install_packages() {
    info "Gerekli sistem paketleri kuruluyor (python3, ffmpeg, git)..."
    case "$PKG" in
        apt-get)
            $SUDO apt-get update -y
            $SUDO apt-get install -y python3 python3-venv python3-pip ffmpeg git
            ;;
        dnf)
            $SUDO dnf install -y python3 python3-pip git
            if ! $SUDO dnf install -y ffmpeg; then
                warn "ffmpeg depoda yok. RHEL/Rocky/Alma için RPM Fusion gerekebilir:"
                warn "  sudo dnf install -y https://download1.rpmfusion.org/free/el/rpmfusion-free-release-\$(rpm -E %rhel).noarch.rpm"
                warn "  Fedora için: sudo dnf install -y ffmpeg --allowerasing"
            fi
            ;;
        zypper)
            $SUDO zypper --non-interactive install python3 python3-pip git ffmpeg \
                || warn "ffmpeg openSUSE'da Packman deposu gerektirebilir."
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
            warn "Paket yöneticisi bulunamadı; python3/ffmpeg/git'i elle kurmanız gerekebilir."
            ;;
    esac
}
install_packages

# ── 3. Python sürüm kontrolü ─────────────────────────────────────────────────
PYTHON=""
for c in python3 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$c" >/dev/null 2>&1; then
        if "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
            PYTHON="$c"; break
        fi
    fi
done
[ -n "$PYTHON" ] || die "Python 3.10+ bulunamadı. Lütfen elle kurun."
ok "Python bulundu: $($PYTHON --version 2>&1)"

command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg hazır." || warn "ffmpeg bulunamadı — ses indirme çalışmayabilir."

# ── 4. venv + bağımlılıklar ──────────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    info "Sanal ortam oluşturuluyor: $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR" || die "venv oluşturulamadı (python3-venv kurulu mu?)."
else
    info "Mevcut sanal ortam kullanılıyor: $VENV_DIR"
fi

VPY="$VENV_DIR/bin/python"
info "pip güncelleniyor ve requirements.txt yükleniyor..."
"$VPY" -m pip install --upgrade pip >/dev/null 2>&1 || warn "pip güncellenemedi, devam ediliyor."
"$VPY" -m pip install -r "$PROJECT_DIR/requirements.txt" || die "Bağımlılıklar yüklenemedi."
ok "Bağımlılıklar yüklendi (yt-dlp, gallery-dl, python-telegram-bot...)."

mkdir -p "$PROJECT_DIR/data/downloads" "$PROJECT_DIR/data/logs"

# ── 5. İnteraktif yapılandırma (data/.env) ───────────────────────────────────
echo
printf "${C_BOLD}── Yapılandırma ──────────────────────────────────────────────${C_RESET}\n"

WRITE_ENV="y"
if [ -f "$ENV_FILE" ]; then
    if ! ask_yn "data/.env zaten var. Yeniden yapılandırılsın mı?" "n"; then
        WRITE_ENV="n"
        info "Mevcut data/.env korunuyor."
    fi
fi

if [ "$WRITE_ENV" = "y" ]; then
    # Zorunlu alanlar
    BOT_TOKEN=""
    while [ -z "$BOT_TOKEN" ]; do
        BOT_TOKEN="$(ask "Bot Token (@BotFather)")"
        [ -z "$BOT_TOKEN" ] && warn "Bot token zorunlu."
    done

    ADMIN_ID=""
    while ! printf '%s' "$ADMIN_ID" | grep -Eq '^[0-9]+$'; do
        ADMIN_ID="$(ask "Admin Telegram ID (sayısal, @userinfobot)")"
        printf '%s' "$ADMIN_ID" | grep -Eq '^[0-9]+$' || warn "Sayısal bir ID girin."
    done

    # Bot adı (herkes kendi botunun adını girer)
    BOT_NAME="$(ask "Bot adı" "Downloader")"

    # Start menüsü linkleri
    SHOW_LINKS="false"; OWNER_LINK=""; COMMUNITY_LINK=""; COMMUNITY_LABEL="Topluluk"
    if ask_yn "Start menüsünde owner/topluluk linkleri gösterilsin mi?" "n"; then
        SHOW_LINKS="true"
        OWNER_LINK="$(ask "Owner linki (örn. https://t.me/kullanici, boş geçilebilir)")"
        COMMUNITY_LINK="$(ask "Topluluk/kanal linki (boş geçilebilir)")"
        if [ -n "$COMMUNITY_LINK" ]; then
            COMMUNITY_LABEL="$(ask "Topluluk butonu etiketi" "Topluluk")"
        fi
    fi

    # Local Bot API (opsiyonel)
    LOCAL_BOT_API_BASE=""; TELEGRAM_API_ID=""; TELEGRAM_API_HASH=""; MAX_FILE_SIZE_MB="1900"
    if ask_yn "50 MB üstü dosyalar için Local Bot API kullanılacak mı?" "n"; then
        LOCAL_BOT_API_BASE="$(ask "Local Bot API adresi" "http://127.0.0.1:8081")"
        TELEGRAM_API_ID="$(ask "TELEGRAM_API_ID (my.telegram.org)")"
        TELEGRAM_API_HASH="$(ask "TELEGRAM_API_HASH")"
        MAX_FILE_SIZE_MB="$(ask "Maksimum dosya boyutu (MB)" "1900")"
    else
        info "Local Bot API kapalı — Telegram 50 MB sınırı geçerli olacak."
    fi

    MAX_SIMULTANEOUS_DOWNLOADS="$(ask "Aynı anda maksimum indirme sayısı" "3")"

    info "data/.env yazılıyor..."
    cat > "$ENV_FILE" <<EOF
# Downloader Bot yapılandırması — install.sh tarafından oluşturuldu
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

CLEANUP_TZ_OFFSET="-3"
CLEANUP_HOUR="0"
LOG_RETENTION_DAYS="7"
CACHE_ENABLED="true"
EOF
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    ok "Yapılandırma kaydedildi: $ENV_FILE"
fi

# ── 6. Hızlı doğrulama ───────────────────────────────────────────────────────
info "Kurulum doğrulanıyor (import testi)..."
if "$VPY" -c "import bot.app" 2>/dev/null; then
    ok "Modüller sorunsuz yükleniyor."
else
    warn "Import testi başarısız oldu; bağımlılıkları kontrol edin."
fi

# ── 7. Otomatik başlatma (opsiyonel) ─────────────────────────────────────────
SERVICE_NAME="downloader-bot"
echo
printf "${C_BOLD}── Otomatik Başlatma ─────────────────────────────────────────${C_RESET}\n"

setup_systemd() {
    local unit="/etc/systemd/system/${SERVICE_NAME}.service"
    info "systemd servisi oluşturuluyor: $unit"
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
    ok "Servis aktif. Durum: sudo systemctl status ${SERVICE_NAME}"
    info "Canlı log: journalctl -u ${SERVICE_NAME} -f"
}

setup_openrc() {
    local script="/etc/init.d/${SERVICE_NAME}"
    info "OpenRC servisi oluşturuluyor: $script"
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
    ok "OpenRC servisi eklendi ve başlatıldı."
}

setup_launchd() {
    local plist="$HOME/Library/LaunchAgents/com.${SERVICE_NAME}.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    info "launchd agent oluşturuluyor: $plist"
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
    ok "launchd agent yüklendi (açılışta otomatik başlar)."
}

setup_rcd() {
    local script="/usr/local/etc/rc.d/${SERVICE_NAME}"
    info "FreeBSD rc.d servisi oluşturuluyor: $script"
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
    ok "rc.d servisi eklendi ve başlatıldı."
}

if ask_yn "Bot sistem açılışında otomatik başlatılsın mı?" "y"; then
    case "$INIT" in
        systemd) setup_systemd ;;
        openrc)  setup_openrc ;;
        launchd) setup_launchd ;;
        rcd)     setup_rcd ;;
        *)
            warn "Otomatik başlatma için desteklenen bir init sistemi bulunamadı."
            warn "Botu elle başlatabilirsiniz:"
            printf "    %s %s\n" "$VPY" "$PROJECT_DIR/start.py"
            ;;
    esac
else
    info "Otomatik başlatma atlandı. Elle başlatmak için:"
    printf "    %s %s\n" "$VPY" "$PROJECT_DIR/start.py"
fi

echo
ok "Kurulum tamamlandı."
