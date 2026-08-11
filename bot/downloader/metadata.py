from __future__ import annotations

"""
bot/downloader/metadata.py — ses dosyalarına doğru metadata + kapak yazar.

Neden gerekli:
    yt-dlp'nin FFmpegExtractAudio adımı dosyaya yalnızca kodlayıcı etiketini
    (TSSE) bırakıyordu; başlık/sanatçı/albüm/yıl hiç yazılmıyordu. Kaynakta
    bu bilgiler MEVCUT (track, artist, artists[], album, release_year) ama
    hiçbiri dosyaya geçmiyordu.

    EmbedThumbnail ise YouTube'un 16:9 video küçük resmini olduğu gibi gömüyor
    (1280x720 PNG, ~630 KB). Müzik çalarlar kapağı kare bekler; sonuç siyah
    bantlı, şişkin bir kapak oluyordu.

Kurallar:
    • UYDURMA YOK. Bir alan kaynakta yoksa boş bırakılır, tahmin edilmez.
    • Kapak kareye ortadan kırpılır ve JPEG'e çevrilir (ffmpeg ile, ek
      bağımlılık yok).
"""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("downloader")

# Kapak boyutu: müzik çalarlar için yeterli, dosyayı şişirmeyecek kadar küçük.
COVER_SIZE = 800
COVER_QUALITY = "3"  # ffmpeg -q:v (2=en iyi, 5=orta)


def _clean(value: Any) -> str:
    """Metni normalize eder; anlamsız değerleri boş sayar."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        parts = [_clean(v) for v in value]
        return ", ".join(p for p in parts if p)
    text = str(value).strip()
    # yt-dlp bazen bu değerleri gerçek veri yerine koyar.
    if text.lower() in {"", "none", "null", "na", "n/a", "unknown", "nan"}:
        return ""
    return text


def extract_music_tags(info: dict[str, Any]) -> dict[str, str]:
    """
    yt-dlp info dict'inden ses etiketlerini çıkarır.

    Yalnızca kaynakta GERÇEKTEN bulunan alanlar döner; eksik alan hiç
    eklenmez (boş string ile doldurulmaz).
    """
    if not isinstance(info, dict):
        return {}

    tags: dict[str, str] = {}

    # ── Başlık: müzikte "track" daha doğrudur, yoksa "title" ──
    title = _clean(info.get("track")) or _clean(info.get("title"))
    if title:
        tags["title"] = title

    # ── Sanatçı: artists[] listesi en doğru kaynak ──
    # Sıra: artists[] → artist → creator → uploader/channel
    # (uploader YouTube Music'te kanal = sanatçıdır; en son çare olarak kullanılır)
    artist = (
        _clean(info.get("artists"))
        or _clean(info.get("artist"))
        or _clean(info.get("creator"))
        or _clean(info.get("uploader"))
        or _clean(info.get("channel"))
    )
    if artist:
        tags["artist"] = artist

    # ── Albüm sanatçısı: varsa alan, yoksa ilk sanatçı ──
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

    # ── Yıl: yalnızca YAYIN tarihi kullanılır ──
    # upload_date bilinçli olarak kullanılmaz: yüklenme tarihi şarkının
    # yayın yılı değildir, yazmak uydurma olur.
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
    Küçük resmi kare kapağa çevirir (ortadan kırpma) ve JPEG yazar.

    YouTube küçük resmi 16:9'dur; kısa kenara göre ortadan kare kırpmak,
    kapak sanatının merkezini korur. ffmpeg kullanılır — yeni bağımlılık yok.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not Path(source).exists():
        return False

    # crop=min(iw,ih):min(iw,ih) → kısa kenara göre ortadan kare kırp
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
        logger.warning("Kapak dönüştürme başarısız: %s", exc)
        return False

    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        logger.warning(
            "Kapak dönüştürme başarısız: %s",
            (proc.stderr or b"").decode("utf-8", "ignore")[:200],
        )
        return False

    return True


def _find_thumbnail(audio_path: Path) -> Path | None:
    """yt-dlp'nin yazdığı küçük resmi ses dosyasının yanında arar."""
    stem = audio_path.with_suffix("")
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = Path(str(stem) + ext)
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate

    # İsim tam eşleşmiyorsa aynı klasördeki tek resmi kullan.
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

    # Yalnızca elimizde olan alanları yaz; diğerlerine dokunma.
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
            type=3,            # 3 = kapak (ön)
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


def apply_audio_metadata(
    audio_path: str | Path,
    info: dict[str, Any],
    *,
    job_id: str = "",
) -> dict[str, str]:
    """
    Ses dosyasına metadata ve kare kapak yazar.

    Dönüş: yazılan etiketler (log/doğrulama için). Hata durumunda boş dict —
    metadata yazılamaması indirmeyi başarısız SAYMAZ, dosya yine gönderilir.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        return {}

    tags = extract_music_tags(info)
    suffix = audio_path.suffix.lower()

    # ── Kapak hazırla ──
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
        else:
            # Diğer biçimlerde (opus/ogg/flac) mutagen'in genel arayüzü.
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
        logger.warning("JOB %s metadata yazılamadı (%s): %s", job_id, audio_path.name, exc)
        return {}
    finally:
        # Geçici kapak dosyası gönderilecek dosyalar arasına karışmasın.
        if cover_jpg and cover_jpg.exists():
            try:
                cover_jpg.unlink()
            except OSError:
                pass

    return tags
