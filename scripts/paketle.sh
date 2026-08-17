#!/usr/bin/env bash
#
# scripts/paketle.sh — builds a shareable source code archive.
#
# Only files tracked by git go into the archive. Everything in .gitignore
# (tokens, cookies, the database, downloads, logs, venv) is excluded by
# definition — no need to keep a "don't forget to add this" list.
#
# The .git folder is NOT included either: old commits may still contain
# secrets that were later scrubbed, and whoever gets the archive could read
# them back out.
#
# After packaging, the archive's contents are scanned for secret patterns;
# if anything is found, the archive is deleted and the script exits with an
# error.
#
# Usage:  ./scripts/paketle.sh [target_dir]     (default: $HOME)

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

TARGET="${1:-$HOME}"
NAME="downloader-bot-$(date +%Y%m%d)"
ZIP="$TARGET/$NAME.zip"

if [ -n "$(git status --porcelain)" ]; then
    echo "WARNING: there are uncommitted changes; the archive will contain the LAST COMMIT."
    echo
fi

echo "Packaging: $ZIP"
rm -f "$ZIP"
git archive --format=zip --prefix="$NAME/" -o "$ZIP" HEAD

# ── Leak scan ─────────────────────────────────────────────────────────────
# Extracts the archive to a temp dir and searches for real bot token, API
# hash, or private key patterns. If found, the archive is deleted.
echo "Scanning for secrets..."
TMPDIR_SCAN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_SCAN"' EXIT
unzip -q "$ZIP" -d "$TMPDIR_SCAN"

# Telegram bot token: 8-10 digits, a colon, 35 characters.
# The "123456789:AAxxxx..." placeholder in example.env is deliberately excluded.
FOUND="$(grep -rEIn \
    -e '[0-9]{8,10}:AA[A-Za-z0-9_-]{33}' \
    -e 'API_HASH[[:space:]]*=[[:space:]]*"[a-f0-9]{32}"' \
    -e 'BEGIN [A-Z ]*PRIVATE KEY' \
    "$TMPDIR_SCAN" 2>/dev/null | grep -v 'AAxxxx' || true)"

if [ -n "$FOUND" ]; then
    echo
    echo "STOPPED — secrets found in the archive:"
    echo "$FOUND"
    rm -f "$ZIP"
    exit 1
fi

# Did runtime data leak in?
LEAKED="$(find "$TMPDIR_SCAN" \( -name 'cookies.txt' -o -name '*.db' -o -name '.env' \
    -o -name 'env-backup.env' -o -name '*.binlog' \) -print 2>/dev/null || true)"
if [ -n "$LEAKED" ]; then
    echo
    echo "STOPPED — runtime data found in the archive:"
    echo "$LEAKED"
    rm -f "$ZIP"
    exit 1
fi

echo "Clean."
echo
echo "Ready: $ZIP  ($(du -h "$ZIP" | cut -f1), $(unzip -l "$ZIP" | tail -1 | awk '{print $2}') files)"
