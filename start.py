from __future__ import annotations

import os


# Hatalı sistem proxy değişkenleri Telegram bağlantısını bozabilir; temizle.
for _key in (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
):
    os.environ.pop(_key, None)


from bot.app import run_bot
from bot.config import load_config


def main() -> None:
    run_bot(load_config())


if __name__ == "__main__":
    main()
