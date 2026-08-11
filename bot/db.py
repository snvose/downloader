from __future__ import annotations

"""
bot/db.py — SQLite veritabanı katmanı.

Neden SQLite: mevcut VDS'te (4 çekirdek, 7.7 GB RAM, tek bot süreci) ayrı bir
veritabanı sunucusu çalıştırmak gereksiz. SQLite tek dosya, sıfır bakım.

Postgres'e geçiş için tasarım kuralları (ölçek büyürse):
  • Tüm SQL bu dosyada toplanır — başka hiçbir modül SQL yazmaz.
  • Placeholder olarak self.ph kullanılır (SQLite "?", Postgres "%s").
  • SQLite'a özel sözdizimi yok: AUTOINCREMENT, INSERT OR REPLACE gibi
    ifadeler yerine taşınabilir ANSI karşılıkları tercih edilir.
  • Zaman alanları epoch saniye (REAL) — zaman dilimi belirsizliği yok,
    her iki veritabanında da aynı çalışır.
  • Şema sürümü user_version yerine ayrı `schema_meta` tablosunda tutulur.

Eşzamanlılık: bot tek süreç + asyncio. Bağlantı paylaşılır, yazmalar bir
kilit ile serileştirilir. Çağrılar bloklayıcıdır → asyncio.to_thread ile
çağrılmalıdır (bkz. bot/handlers, asyncio.to_thread kullanımı).
"""

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("downloader")

SCHEMA_VERSION = 1


class Database:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ph = "?"  # Postgres'e geçişte "%s"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            timeout=15.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self.migrate()

    def _configure(self) -> None:
        with self._lock:
            # WAL: okuma ve yazma birbirini bloklamaz — bot yanıt vermeye devam eder.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=15000")

    # ── Şema ─────────────────────────────────────────────────────────────────

    def migrate(self) -> None:
        """Şemayı oluşturur/günceller. Tekrar çağrılması güvenlidir."""
        with self._lock:
            cur = self._conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            # ── Kullanıcılar ──
            # broadcast_opt_out: Faz 3 duyuru sistemi bunu okuyacak.
            # is_blocked: kullanıcı botu engellediyse duyuru gönderilmez
            #             (Telegram "bot was blocked" hatasında işaretlenir).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id            INTEGER PRIMARY KEY,
                    username           TEXT,
                    first_name         TEXT,
                    language           TEXT,
                    first_seen         REAL NOT NULL,
                    last_activity      REAL NOT NULL,
                    total_downloads    INTEGER NOT NULL DEFAULT 0,
                    broadcast_opt_out  INTEGER NOT NULL DEFAULT 0,
                    is_blocked         INTEGER NOT NULL DEFAULT 0
                )
            """)

            # ── Sohbetler (özel + grup) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id            INTEGER PRIMARY KEY,
                    title              TEXT,
                    chat_type          TEXT,
                    first_seen         REAL NOT NULL,
                    last_activity      REAL NOT NULL,
                    total_downloads    INTEGER NOT NULL DEFAULT 0,
                    broadcast_opt_out  INTEGER NOT NULL DEFAULT 0,
                    is_blocked         INTEGER NOT NULL DEFAULT 0
                )
            """)

            # ── Platform bazlı sayaç (chats.json'daki JSON blob'un normali) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_platforms (
                    chat_id   INTEGER NOT NULL,
                    platform  TEXT NOT NULL,
                    count     INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, platform)
                )
            """)

            # ── İndirme geçmişi (analitik + hata takibi) ──
            cur.execute("""
                CREATE TABLE IF NOT EXISTS downloads (
                    id          INTEGER PRIMARY KEY,
                    created_at  REAL NOT NULL,
                    user_id     INTEGER,
                    chat_id     INTEGER,
                    platform    TEXT,
                    url         TEXT,
                    mode        TEXT,
                    source      TEXT,
                    result      TEXT,
                    file_size   INTEGER,
                    duration    REAL
                )
            """)

            for stmt in (
                "CREATE INDEX IF NOT EXISTS idx_users_activity ON users(last_activity)",
                "CREATE INDEX IF NOT EXISTS idx_users_broadcast ON users(broadcast_opt_out, is_blocked)",
                "CREATE INDEX IF NOT EXISTS idx_chats_activity ON chats(last_activity)",
                "CREATE INDEX IF NOT EXISTS idx_chats_type ON chats(chat_type)",
                "CREATE INDEX IF NOT EXISTS idx_dl_created ON downloads(created_at)",
                "CREATE INDEX IF NOT EXISTS idx_dl_user ON downloads(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_dl_platform ON downloads(platform)",
            ):
                cur.execute(stmt)

            cur.execute(
                f"INSERT OR REPLACE INTO schema_meta (key, value) VALUES ({self.ph}, {self.ph})",
                ("version", str(SCHEMA_VERSION)),
            )
            self._conn.commit()

    # ── Düşük seviye yardımcılar ─────────────────────────────────────────────

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ── Kullanıcı ────────────────────────────────────────────────────────────

    def touch_user(
        self,
        user_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        language: str | None = None,
    ) -> None:
        """Kullanıcıyı kaydeder/günceller (her etkileşimde çağrılır)."""
        now = time.time()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"SELECT user_id FROM users WHERE user_id = {self.ph}", (user_id,)
            )
            if cur.fetchone():
                cur.execute(
                    f"""UPDATE users SET
                           last_activity = {self.ph},
                           username   = COALESCE({self.ph}, username),
                           first_name = COALESCE({self.ph}, first_name),
                           language   = COALESCE({self.ph}, language),
                           is_blocked = 0
                        WHERE user_id = {self.ph}""",
                    (now, username, first_name, language, user_id),
                )
            else:
                cur.execute(
                    f"""INSERT INTO users
                           (user_id, username, first_name, language,
                            first_seen, last_activity)
                        VALUES ({self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph})""",
                    (user_id, username, first_name, language, now, now),
                )
            self._conn.commit()

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        return self.query_one(
            f"SELECT * FROM users WHERE user_id = {self.ph}", (user_id,)
        )

    def set_broadcast_opt_out(self, *, user_id: int, opt_out: bool) -> None:
        """Kullanıcının duyuru tercihi (Faz 3)."""
        self.execute(
            f"UPDATE users SET broadcast_opt_out = {self.ph} WHERE user_id = {self.ph}",
            (1 if opt_out else 0, user_id),
        )

    def mark_blocked(self, *, user_id: int | None = None, chat_id: int | None = None) -> None:
        """Telegram 'bot engellendi' dediğinde çağrılır — duyuru listesinden düşer."""
        if user_id is not None:
            self.execute(
                f"UPDATE users SET is_blocked = 1 WHERE user_id = {self.ph}", (user_id,)
            )
        if chat_id is not None:
            self.execute(
                f"UPDATE chats SET is_blocked = 1 WHERE chat_id = {self.ph}", (chat_id,)
            )

    # ── Sohbet ───────────────────────────────────────────────────────────────

    def touch_chat(
        self,
        chat_id: int,
        *,
        title: str | None = None,
        chat_type: str | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(f"SELECT chat_id FROM chats WHERE chat_id = {self.ph}", (chat_id,))
            if cur.fetchone():
                cur.execute(
                    f"""UPDATE chats SET
                           last_activity = {self.ph},
                           title     = COALESCE({self.ph}, title),
                           chat_type = COALESCE({self.ph}, chat_type),
                           is_blocked = 0
                        WHERE chat_id = {self.ph}""",
                    (now, title, chat_type, chat_id),
                )
            else:
                cur.execute(
                    f"""INSERT INTO chats
                           (chat_id, title, chat_type, first_seen, last_activity)
                        VALUES ({self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph})""",
                    (chat_id, title, chat_type, now, now),
                )
            self._conn.commit()

    # ── İndirme kaydı ────────────────────────────────────────────────────────

    def record_download(
        self,
        *,
        user_id: int | None,
        chat_id: int | None,
        platform: str = "",
        url: str = "",
        mode: str = "",
        source: str = "",
        result: str = "success",
        file_size: int = 0,
        duration: float = 0.0,
        username: str | None = None,
        chat_title: str | None = None,
        chat_type: str | None = None,
    ) -> None:
        """
        Bir indirmeyi kaydeder ve ilgili sayaçları günceller.

        Başarılı indirmelerde kullanıcı/sohbet toplamları artar; hatalı
        denemeler yalnızca downloads tablosuna yazılır (istatistik şişmesin).
        """
        now = time.time()

        if user_id:
            self.touch_user(user_id, username=username)
        if chat_id:
            self.touch_chat(chat_id, title=chat_title, chat_type=chat_type)

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"""INSERT INTO downloads
                       (created_at, user_id, chat_id, platform, url, mode,
                        source, result, file_size, duration)
                    VALUES ({self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph},
                            {self.ph}, {self.ph}, {self.ph}, {self.ph}, {self.ph})""",
                (now, user_id, chat_id, platform, url[:500], mode,
                 source, result, int(file_size or 0), float(duration or 0.0)),
            )

            if result == "success":
                if user_id:
                    cur.execute(
                        f"UPDATE users SET total_downloads = total_downloads + 1 "
                        f"WHERE user_id = {self.ph}", (user_id,)
                    )
                if chat_id:
                    cur.execute(
                        f"UPDATE chats SET total_downloads = total_downloads + 1 "
                        f"WHERE chat_id = {self.ph}", (chat_id,)
                    )
                if chat_id and platform:
                    # Taşınabilir upsert: önce güncelle, etkilenen satır yoksa ekle.
                    cur.execute(
                        f"""UPDATE chat_platforms SET count = count + 1
                            WHERE chat_id = {self.ph} AND platform = {self.ph}""",
                        (chat_id, platform),
                    )
                    if cur.rowcount == 0:
                        cur.execute(
                            f"""INSERT INTO chat_platforms (chat_id, platform, count)
                                VALUES ({self.ph}, {self.ph}, 1)""",
                            (chat_id, platform),
                        )

            self._conn.commit()

    # ── Duyuru hedefleri (Faz 3) ─────────────────────────────────────────────

    def broadcast_targets(self, *, kind: str = "all") -> list[int]:
        """
        Duyuru gönderilecek hedefler.

        kind: "users"   → yalnızca özel sohbetler (kullanıcılar)
              "groups"  → yalnızca gruplar
              "all"     → ikisi de

        Opt-out yapmış ve botu engellemiş olanlar hariç tutulur.
        """
        targets: list[int] = []

        if kind in {"users", "all"}:
            rows = self.query(
                "SELECT user_id FROM users "
                "WHERE broadcast_opt_out = 0 AND is_blocked = 0 "
                "ORDER BY last_activity DESC"
            )
            targets.extend(int(r["user_id"]) for r in rows)

        if kind in {"groups", "all"}:
            rows = self.query(
                "SELECT chat_id FROM chats "
                "WHERE broadcast_opt_out = 0 AND is_blocked = 0 "
                "AND chat_type IN ('group', 'supergroup') "
                "ORDER BY last_activity DESC"
            )
            targets.extend(int(r["chat_id"]) for r in rows)

        return targets

    # ── İstatistik ───────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        def scalar(sql: str, default: Any = 0) -> Any:
            row = self.query_one(sql)
            if not row:
                return default
            value = list(row.values())[0]
            return value if value is not None else default

        return {
            "total_users": scalar("SELECT COUNT(*) FROM users"),
            "total_chats": scalar("SELECT COUNT(*) FROM chats"),
            "groups": scalar(
                "SELECT COUNT(*) FROM chats WHERE chat_type IN ('group','supergroup')"
            ),
            "privates": scalar("SELECT COUNT(*) FROM chats WHERE chat_type = 'private'"),
            "total_downloads": scalar("SELECT COUNT(*) FROM downloads WHERE result='success'"),
            "failed_downloads": scalar("SELECT COUNT(*) FROM downloads WHERE result<>'success'"),
            "opt_out": scalar("SELECT COUNT(*) FROM users WHERE broadcast_opt_out = 1"),
            "blocked": scalar("SELECT COUNT(*) FROM users WHERE is_blocked = 1"),
            "total_bytes": scalar(
                "SELECT COALESCE(SUM(file_size),0) FROM downloads WHERE result='success'"
            ),
        }

    def top_platforms(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.query(
            f"""SELECT platform, COUNT(*) AS count FROM downloads
                WHERE result='success' AND platform IS NOT NULL AND platform <> ''
                GROUP BY platform ORDER BY count DESC LIMIT {self.ph}""",
            (limit,),
        )

    def search_users(self, term: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Kullanıcı adı, ad veya ID ile arama (admin paneli).

        Sayı girilirse önce tam ID eşleşmesi denenir; bulunamazsa metin
        araması yapılır.
        """
        term = (term or "").strip().lstrip("@")
        if not term:
            return []

        if term.lstrip("-").isdigit():
            row = self.query_one(
                f"SELECT * FROM users WHERE user_id = {self.ph}", (int(term),)
            )
            if row:
                return [row]

        pattern = f"%{term.lower()}%"
        return self.query(
            f"""SELECT * FROM users
                WHERE LOWER(COALESCE(username, '')) LIKE {self.ph}
                   OR LOWER(COALESCE(first_name, '')) LIKE {self.ph}
                ORDER BY total_downloads DESC LIMIT {self.ph}""",
            (pattern, pattern, limit),
        )

    def search_chats(self, term: str, limit: int = 10) -> list[dict[str, Any]]:
        term = (term or "").strip()
        if not term:
            return []

        if term.lstrip("-").isdigit():
            row = self.query_one(
                f"SELECT * FROM chats WHERE chat_id = {self.ph}", (int(term),)
            )
            if row:
                return [row]

        pattern = f"%{term.lower()}%"
        return self.query(
            f"""SELECT * FROM chats WHERE LOWER(COALESCE(title, '')) LIKE {self.ph}
                ORDER BY total_downloads DESC LIMIT {self.ph}""",
            (pattern, limit),
        )

    def user_downloads(self, user_id: int, limit: int = 5) -> list[dict[str, Any]]:
        """Bir kullanıcının son indirmeleri (profil ekranı)."""
        return self.query(
            f"""SELECT platform, result, created_at, file_size FROM downloads
                WHERE user_id = {self.ph} ORDER BY created_at DESC LIMIT {self.ph}""",
            (user_id, limit),
        )

    def top_chats(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.query(
            f"""SELECT chat_id, title, chat_type, total_downloads, last_activity
                FROM chats ORDER BY total_downloads DESC LIMIT {self.ph}""",
            (limit,),
        )

    def active_since(self, seconds: float) -> dict[str, int]:
        cutoff = time.time() - seconds
        return {
            "users": len(self.query(
                f"SELECT user_id FROM users WHERE last_activity >= {self.ph}", (cutoff,)
            )),
            "chats": len(self.query(
                f"SELECT chat_id FROM chats WHERE last_activity >= {self.ph}", (cutoff,)
            )),
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
