from __future__ import annotations

"""
Writes accurate metadata and a cover image onto downloaded audio files.

Why this exists:
    yt-dlp's FFmpegExtractAudio step only left an encoder tag (TSSE) on the
    file; title/artist/album/year were never written, even though the source
    info HAS them (track, artist, artists[], album, release_year).

    EmbedThumbnail embeds YouTube's 16:9 video thumbnail as-is (1280x720 PNG,
    ~630 KB). Music players expect a square cover, so the result was a
    bloated cover with black bars.

Rules:
    • NO GUESSING. A field missing from the source is left empty, never
      invented.
    • The cover is centre-cropped to a square and converted to JPEG (via
      ffmpeg, no extra dependency).
"""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("downloader")

# Cover size: enough for music players, small enough not to bloat the file.
COVER_SIZE = 800
COVER_QUALITY = "3"  # ffmpeg -q:v (2=best, 5=medium)


def _clean(value: Any) -> str:
    """Normalizes text; treats meaningless values as empty."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        parts = [_clean(v) for v in value]
        return ", ".join(p for p in parts if p)
    text = str(value).strip()
    # yt-dlp sometimes puts these placeholders in instead of real data.
    if text.lower() in {"", "none", "null", "na", "n/a", "unknown", "nan"}:
        return ""
    return text


def extract_music_tags(info: dict[str, Any]) -> dict[str, str]:
    """
    Extracts audio tags from a yt-dlp info dict.

    Only fields that ACTUALLY exist in the source are returned; a missing
    field is never added (not even as an empty string).
    """
    if not isinstance(info, dict):
        return {}

    tags: dict[str, str] = {}

    # ── Title: "track" is more accurate for music, else "title" ──
    title = _clean(info.get("track")) or _clean(info.get("title"))
    if title:
        tags["title"] = title

    # ── Artist: the artists[] list is the most accurate source ──
    # Order: artists[] -> artist -> creator -> uploader/channel
    # (on YouTube Music, uploader/channel is the artist; used as a last resort)
    artist = (
        _clean(info.get("artists"))
        or _clean(info.get("artist"))
        or _clean(info.get("creator"))
        or _clean(info.get("uploader"))
        or _clean(info.get("channel"))
    )
    if artist:
        tags["artist"] = artist

    # ── Album artist: use the field if present, else the first artist ──
    album_artist = _clean(info.get("album_artist"))
    if not album_artist:
        artists = info.get("artists")
        if isinstance(artists, (list, tuple)) and artists:
            album_artist = _clean(artists[0])
    if album_artist:
        tags["album_artist"] = album_artist

    album = _clean(info.get("album"))
    if album:
        tags["album"] = album

    # ── Year: only the RELEASE date is used ──
    # upload_date is deliberately ignored: the upload date is not the
    # track's release year, and writing it would be a guess.
    year = ""
    release_year = info.get("release_year")
    if release_year:
        year = _clean(release_year)[:4]
    if not year:
        release_date = _clean(info.get("release_date"))
        if len(release_date) >= 4 and release_date[:4].isdigit():
            year = release_date[:4]
    if year and year.isdigit():
        tags["date"] = year

    track_number = info.get("track_number")
    if track_number not in (None, ""):
        try:
            number = int(track_number)
            if number > 0:
                tags["track_number"] = str(number)
        except (TypeError, ValueError):
            pass

    genre = _clean(info.get("genre"))
    if genre:
        tags["genre"] = genre

    return tags


def make_square_cover(source: Path, dest: Path, *, size: int = COVER_SIZE) -> bool:
    """
    Converts a thumbnail into a square cover (centre crop) and writes JPEG.

    YouTube thumbnails are 16:9; cropping to a centred square keyed off the
    short edge keeps the artwork's centre. Uses ffmpeg — no new dependency.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not Path(source).exists():
        return False

    # crop=min(iw,ih):min(iw,ih) -> centred square crop off the short edge
    vf = f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}"

    try:
        proc = subprocess.run(
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-i", str(source),
                "-vf", vf,
                "-q:v", COVER_QUALITY,
                "-frames:v", "1",
                str(dest),
            ],
            capture_output=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Cover conversion failed: %s", exc)
        return False

    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        logger.warning(
            "Cover conversion failed: %s",
            (proc.stderr or b"").decode("utf-8", "ignore")[:200],
        )
        return False

    return True


def _find_thumbnail(audio_path: Path) -> Path | None:
    """Looks for the thumbnail yt-dlp wrote next to the audio file."""
    stem = audio_path.with_suffix("")
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = Path(str(stem) + ext)
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate

    # If the name doesn't match exactly, use the single image in the folder.
    try:
        images = [
            p for p in audio_path.parent.iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            and p.stat().st_size > 0
        ]
    except OSError:
        return None

    return images[0] if len(images) == 1 else None


def _write_mp3(path: Path, tags: dict[str, str], cover: Path | None) -> None:
    from mutagen.id3 import (
        APIC, ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK,
    )

    try:
        audio = ID3(str(path))
    except ID3NoHeaderError:
        audio = ID3()

    # Only write the fields we actually have; leave the rest untouched.
    frames = {
        "title": (TIT2, "title"),
        "artist": (TPE1, "artist"),
        "album_artist": (TPE2, "album_artist"),
        "album": (TALB, "album"),
        "date": (TDRC, "date"),
        "track_number": (TRCK, "track_number"),
        "genre": (TCON, "genre"),
    }

    for key, (frame_cls, _name) in frames.items():
        value = tags.get(key)
        if value:
            audio.setall(frame_cls.__name__, [frame_cls(encoding=3, text=[value])])

    if cover and cover.exists():
        audio.delall("APIC")
        audio.add(APIC(
            encoding=0,
            mime="image/jpeg",
            type=3,            # 3 = front cover
            desc="Cover",
            data=cover.read_bytes(),
        ))

    audio.save(str(path), v2_version=3)


def _write_mp4(path: Path, tags: dict[str, str], cover: Path | None) -> None:
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(str(path))

    mapping = {
        "title": "\xa9nam",
        "artist": "\xa9ART",
        "album_artist": "aART",
        "album": "\xa9alb",
        "date": "\xa9day",
        "genre": "\xa9gen",
    }
    for key, atom in mapping.items():
        value = tags.get(key)
        if value:
            audio[atom] = [value]

    if tags.get("track_number"):
        try:
            audio["trkn"] = [(int(tags["track_number"]), 0)]
        except (TypeError, ValueError):
            pass

    if cover and cover.exists():
        audio["covr"] = [MP4Cover(cover.read_bytes(), imageformat=MP4Cover.FORMAT_JPEG)]

    audio.save()


def _write_flac(path: Path, tags: dict[str, str], cover: Path | None) -> None:
    """
    Writes Vorbis comments and a cover onto a FLAC file.

    mutagen's generic interface writes FLAC tags but NOT the cover: a FLAC
    cover is a separate PICTURE block that can't be assigned through the
    dict interface. Hence FLAC gets its own writer — otherwise flac files
    came out with no cover at all.
    """
    from mutagen.flac import FLAC, Picture

    audio = FLAC(str(path))

    mapping = {
        "title": "title",
        "artist": "artist",
        "album_artist": "albumartist",
        "album": "album",
        "date": "date",
        "genre": "genre",
        "track_number": "tracknumber",
    }
    for key, field in mapping.items():
        value = tags.get(key)
        if value:
            audio[field] = [str(value)]

    if cover and cover.exists():
        picture = Picture()
        picture.data = cover.read_bytes()
        picture.type = 3  # front cover
        picture.mime = "image/jpeg"
        audio.clear_pictures()
        audio.add_picture(picture)

    audio.save()


def apply_audio_metadata(
    audio_path: str | Path,
    info: dict[str, Any],
    *,
    job_id: str = "",
) -> dict[str, str]:
    """
    Writes metadata and a square cover onto an audio file.

    Returns the tags that were written (for logging/verification). An empty
    dict on error — a metadata write failure does NOT fail the download,
    the file is still sent.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        return {}

    tags = extract_music_tags(info)
    suffix = audio_path.suffix.lower()

    # ── Prepare the cover ──
    cover_jpg: Path | None = None
    source_thumb = _find_thumbnail(audio_path)
    if source_thumb:
        candidate = audio_path.parent / f"__cover_{audio_path.stem[:40]}.jpg"
        if make_square_cover(source_thumb, candidate):
            cover_jpg = candidate

    try:
        if suffix == ".mp3":
            _write_mp3(audio_path, tags, cover_jpg)
        elif suffix in {".m4a", ".mp4", ".aac"}:
            _write_mp4(audio_path, tags, cover_jpg)
        elif suffix == ".flac":
            _write_flac(audio_path, tags, cover_jpg)
        else:
            # Other formats (opus/ogg) use mutagen's generic interface.
            from mutagen import File as MutagenFile
            audio = MutagenFile(str(audio_path))
            if audio is not None:
                for key, value in tags.items():
                    try:
                        audio[key] = [value]
                    except Exception:
                        pass
                audio.save()
    except Exception as exc:
        logger.warning("JOB %s metadata write failed (%s): %s", job_id, audio_path.name, exc)
        return {}
    finally:
        # Don't let the temporary cover file end up among the sent files.
        if cover_jpg and cover_jpg.exists():
            try:
                cover_jpg.unlink()
            except OSError:
                pass

    return tags
