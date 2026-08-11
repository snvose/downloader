#!/usr/bin/env bash
#
# scripts/paketle.sh — paylaşılabilir kaynak kodu arşivi üretir.
#
# Arşive YALNIZCA git'in takip ettiği dosyalar girer. Böylece .gitignore'daki
# her şey (token, cookie, veritabanı, indirmeler, loglar, venv) tanım gereği
# dışarıda kalır — "şunu da eklemeyi unutmayayım" listesi tutmaya gerek yok.
#
# .git klasörü de arşive GİRMEZ: geçmiş commit'lerde eskiden temizlenmiş
# sırlar durabilir ve arşivi alan kişi onları geri okuyabilir.
#
# Paketledikten sonra arşivin içi sır kalıpları için taranır; bir şey
# bulunursa arşiv silinir ve script hata ile çıkar.
#
# Kullanım:  ./scripts/paketle.sh [hedef_klasör]     (varsayılan: $HOME)

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

HEDEF="${1:-$HOME}"
AD="downloader-bot-$(date +%Y%m%d)"
ZIP="$HEDEF/$AD.zip"

if [ -n "$(git status --porcelain)" ]; then
    echo "UYARI: commit edilmemiş değişiklikler var; arşive SON COMMIT girer."
    echo
fi

echo "Paketleniyor: $ZIP"
rm -f "$ZIP"
git archive --format=zip --prefix="$AD/" -o "$ZIP" HEAD

# ── Sızıntı taraması ─────────────────────────────────────────────────────────
# Arşiv içeriğini geçici bir yere açıp gerçek bot token'ı, api hash, özel
# anahtar gibi kalıpları arar. Bulursa arşivi silip durur.
echo "Sır taraması..."
GECICI="$(mktemp -d)"
trap 'rm -rf "$GECICI"' EXIT
unzip -q "$ZIP" -d "$GECICI"

# Telegram bot token'ı: 8-10 hane, iki nokta, 35 karakter.
# example.env'deki "123456789:AAxxxx..." yer tutucusu kasıtlı olarak elenir.
BULUNAN="$(grep -rEIn \
    -e '[0-9]{8,10}:AA[A-Za-z0-9_-]{33}' \
    -e 'API_HASH[[:space:]]*=[[:space:]]*"[a-f0-9]{32}"' \
    -e 'BEGIN [A-Z ]*PRIVATE KEY' \
    "$GECICI" 2>/dev/null | grep -v 'AAxxxx' || true)"

if [ -n "$BULUNAN" ]; then
    echo
    echo "DURDURULDU — arşivde sır bulundu:"
    echo "$BULUNAN"
    rm -f "$ZIP"
    exit 1
fi

# Çalışma zamanı verisi kaçak girmiş mi?
KACAK="$(find "$GECICI" \( -name 'cookies.txt' -o -name '*.db' -o -name '.env' \
    -o -name 'env-backup.env' -o -name '*.binlog' \) -print 2>/dev/null || true)"
if [ -n "$KACAK" ]; then
    echo
    echo "DURDURULDU — arşivde çalışma zamanı verisi var:"
    echo "$KACAK"
    rm -f "$ZIP"
    exit 1
fi

echo "Temiz."
echo
echo "Hazır: $ZIP  ($(du -h "$ZIP" | cut -f1), $(unzip -l "$ZIP" | tail -1 | awk '{print $2}') dosya)"
