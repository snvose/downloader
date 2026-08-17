# cobalt integration

## Why

cobalt is noticeably faster than yt-dlp on some platforms. Real numbers
measured on this VDS:

| Platform  | cobalt | yt-dlp | Winner |
| --------- | -----: | -----: | ------ |
| TikTok    | 1.2 s | 5.2 s | cobalt, ~4x faster |
| YouTube   | doesn't work (see below) | 13.7 s | yt-dlp |
| Instagram | doesn't work (needs login) | 4.7 s | yt-dlp |

cobalt resolves content server-side; it stays more stable on platforms that
change their HTML often (TikTok/Twitter) and returns watermark-free results.

## No public API

cobalt has no official shared API:

> "there is currently no publicly available pre-hosted api"
> — [api/README.md](https://github.com/imputnet/cobalt/blob/main/api/README.md)

So you have to **run your own instance**. If `COBALT_API_URL` is left empty,
the cobalt source is silently skipped and the bot runs on yt-dlp +
gallery-dl alone. This integration is optional; the bot works fully without
cobalt.

## Setup

```bash
docker run -d --name cobalt --restart unless-stopped \
  -p 127.0.0.1:9000:9000 \
  -e API_URL="http://127.0.0.1:9000/" \
  --memory=1g --cpus=1 \
  ghcr.io/imputnet/cobalt:11
```

Then in `data/.env`:

```env
COBALT_API_URL=http://127.0.0.1:9000
# COBALT_API_KEY=...    # if your instance requires auth
COBALT_TIMEOUT=30
```

**Don't expose the port.** Binding to `127.0.0.1:9000` is deliberate: opening
cobalt to the internet both triggers the AGPL obligation below and turns it
into an open download proxy.

## Why YouTube doesn't work

cobalt gets blocked by YouTube when fetching from a datacenter IP; the
tunnel returns HTTP 200 but **0 bytes**. The fix is running a separate
`YOUTUBE_SESSION_SERVER` (a po_token generator) — extra complexity.

That's why the default priority puts **yt-dlp first** for YouTube/YouTube
Music. The client also treats a 0-byte result as an error and moves to the
next source, so even without a session server the user never gets an empty
file.

## Source order

Managed through `data/sources.json`, editable **without touching code**
(a changed file takes effect without restarting the bot):

```json
{
  "default": ["ytdlp", "cobalt", "gallerydl"],
  "platforms": {
    "TikTok":    ["cobalt", "ytdlp", "gallerydl"],
    "Instagram": ["cobalt", "ytdlp", "gallerydl"],
    "YouTube":   ["ytdlp", "cobalt"]
  }
}
```

If a source errors, the next one is tried. cobalt drops out of the list
automatically when it's not configured or doesn't support the platform.

## License — AGPL-3.0 (important)

cobalt is **AGPL-3.0** licensed. The licensing status of this integration:

**This bot is not bound by AGPL through cobalt.** `bot/downloader/cobalt.py`
does not contain, copy, or link cobalt's source code; it only makes HTTP
requests to a separate service. Talking to a separate program over the
network does not create a derivative work.

**But if you host the cobalt instance yourself, you have an obligation.**
AGPL-3.0 §13 requires anyone who offers the software to users over a network
to make the source code available to those users:

> "if you modify the Program, your modified version must prominently offer all
> users interacting with it remotely through a computer network ... an
> opportunity to receive the Corresponding Source of your version"

In practice:

1. **If you run cobalt unmodified from the official image** (the docker
   command above), the source is already public upstream. There's no extra
   obligation in practice; it's still good form to mention in the bot's
   `/help` or `/start` text that cobalt is used, with a link upstream.
2. **If you modify cobalt** (a fork, a patch, an added service), you
   **must** make the modified source available to the bot's users.
3. The bot's own source code is unaffected by any of this — it can stay
   under whatever license you choose.

To eliminate the risk entirely: don't modify cobalt, run the official image.

This bot's own license (AGPL-3.0, see [`LICENSE`](../LICENSE)) is a separate
choice made for this project and applies regardless of the cobalt situation
above.
