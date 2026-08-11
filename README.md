# Downloader Bot

27 platformdan medya indirip Telegram'a yükleyen, çoklu işlem tabanlı bir
Telegram botu. Sade ve profesyonel bir arayüze, kalıcı önbelleğe, mod
sistemine ve yönetici paneline sahiptir.

## Desteklenen Platformlar

Bot üç indirme kaynağı kullanır: **cobalt** (opsiyonel, kendi instance'ın),
**yt-dlp** ve **gallery-dl**. Bir kaynak başarısız olursa sıradaki denenir;
sıra platform bazında `data/sources.json` ile ayarlanır.

| Platform | Video | Ses | Kaynak önceliği | Durum |
|----------|:-----:|:---:|-----------------|-------|
| YouTube | ✅ 1080p–360p | ✅ 320k–128k | yt-dlp → cobalt | ✅ test edildi |
| YouTube Music | — | ✅ | yt-dlp → cobalt | ✅ test edildi |
| Instagram | ✅ | ✅ | cobalt → yt-dlp → gallery-dl | ✅ test edildi |
| TikTok | ✅ | ✅ | cobalt → yt-dlp → gallery-dl | ✅ test edildi |
| X/Twitter | ✅ | ✅ | cobalt → yt-dlp → gallery-dl | ✅ test edildi |
| Reddit | ✅ | ✅ | cobalt → yt-dlp → gallery-dl | ✅ test edildi |
| Pinterest | ✅ | ✅ | cobalt → yt-dlp → gallery-dl | ✅ test edildi |
| Facebook | ✅ | — | cobalt → yt-dlp → gallery-dl | ✅ test edildi |
| Spotify | — | ✅ (YouTube üzerinden) | yt-dlp | ✅ test edildi |
| **SoundCloud** | — | ✅ | yt-dlp → cobalt | ✅ test edildi |
| **Dailymotion** | ✅ | ✅ | yt-dlp → cobalt | ✅ test edildi |
| **Streamable** | ✅ | ✅ | yt-dlp → cobalt | ✅ test edildi |
| **Vimeo** | ✅ | ✅ | yt-dlp → cobalt | ⚠️ yt-dlp'de geçici arıza |
| **Twitch (klip)** | ✅ | ✅ | yt-dlp → cobalt | ⏳ uçtan uca test edilmedi |
| **Bluesky** | ✅ | ✅ | yt-dlp → cobalt | ⏳ uçtan uca test edilmedi |
| **Tumblr** | ✅ | ✅ | yt-dlp → gallery-dl | ⏳ uçtan uca test edilmedi |
| **VK** | ✅ | — | yt-dlp → cobalt | ⏳ uçtan uca test edilmedi |
| **Rutube** | ✅ | ✅ | yt-dlp → cobalt | ⏳ uçtan uca test edilmedi |
| **Bilibili** | ✅ | ✅ | yt-dlp → cobalt | ⏳ uçtan uca test edilmedi |
| **Imgur** | ✅ | — | yt-dlp → gallery-dl | ⏳ uçtan uca test edilmedi |
| **Bandcamp** | — | ✅ | yt-dlp | ⏳ uçtan uca test edilmedi |
| **Mixcloud** | — | ✅ | yt-dlp | ⏳ uçtan uca test edilmedi |
| **Rumble** | ✅ | ✅ | yt-dlp | ⏳ uçtan uca test edilmedi |
| **Newgrounds** | ✅ | ✅ | yt-dlp → cobalt | ⏳ uçtan uca test edilmedi |
| **Loom** | ✅ | — | yt-dlp → cobalt | ⏳ uçtan uca test edilmedi |
| **OK.ru** | ✅ | — | yt-dlp → cobalt | ⏳ uçtan uca test edilmedi |
| **Snapchat** | ✅ | ✅ | cobalt → yt-dlp | ⏳ uçtan uca test edilmedi |
| **Kick** | ✅ | ✅ | yt-dlp | ⏳ uçtan uca test edilmedi |

**Kalın** olanlar Faz 2'de eklendi. "⏳" = araçlar platformu destekliyor ancak
gerçek bir linkle uçtan uca doğrulama yapılmadı.

🔴 **Canlı yayınlar hiçbir platformda desteklenmez** — sonsuz akış olduğu için
indirme başlatılmadan reddedilir.

> yt-dlp 1751, gallery-dl 269 site tanıyor. Yukarıdaki liste bilinçli olarak
> dar tutuldu: yalnızca yaygın kullanılan ve bot arayüzüne uyan platformlar
> açık. Yeni platform eklemek için `bot/utils.py` içindeki `SUPPORTED_DOMAINS`
> ve `platform_name()` güncellenir.

## Özellikler

- **Çok kaynaklı indirme** — cobalt + yt-dlp + gallery-dl; biri başarısız
  olursa diğerine otomatik geçilir (`data/sources.json` ile yapılandırılır).
- **Format seçimi** — YouTube için video (1080p–360p) / ses (320k–128k) menüsü.
- **Altyazı** — YouTube videolarına TR/EN altyazı gömülü indirme.
- **Doğru ses metadata'sı** — başlık, sanatçı, albüm sanatçısı, albüm, yayın
  yılı ve tür kaynaktan okunup dosyaya yazılır (mutagen ID3/MP4). Kapak
  resmi kareye kırpılıp gömülür. Kaynakta olmayan alan boş bırakılır.
- **Cookie sağlık paneli** — `/admin → 🍪 Cookie`: platform bazında çerez
  durumu (geçerli / yakında bitiyor / süresi dolmuş / eksik), kalan gün ve o
  platformda cookie yüzünden başarısız olan istek sayısı. Cookie kaynaklı
  hatalar ayrıca `data/logs/cookie_errors.log` dosyasına yazılır.
- **Canlı yayın koruması** — canlı linkler 2 saniye içinde reddedilir;
  tekrarlayan denemeler uyarı, 3. denemede 5 günlük geçici ban.
- **SQLite veritabanı** — kullanıcı/sohbet kayıtları, ilk görülme, son
  aktivite, kullanım sayıları ve duyuru tercihi (`data/bot.db`).
- **Playlist tarayıcı** — YouTube playlistlerinden seçmeli/toplu indirme.
- **Sade arayüz** — medya altında yalnızca platform logosu ve adı; detaylar tek
  bir "Detaylar" butonuyla açılır.
- **file_id önbelleği** — aynı link ikinci kez gönderilince yeniden indirilmez;
  kayıtlı `file_id` ile anında iletilir, gerekirse diskten yeniden yüklenir.
- **Otomatik temizlik** — `downloads/` klasörü her gün belirlenen saatte
  (varsayılan GMT-3 00:00) temizlenir; silinen dosya ve boşalan alan loglanır.
- **Detaylı loglama** — her indirme; kullanıcı, sohbet, platform, sonuç, boyut
  ve süre bilgisiyle günlük dosyalara yazılır (varsayılan 7 gün saklanır).
- **Çok dilli** — kullanıcı mesajları 7 dilde: Türkçe, English, Русский,
  Deutsch, Español, Français, العربية. Dil `/admin` panelinden seçilir.
- **Çalışma modları** — `normal`, `safe` (sessiz), `maintenance` (bakım).
- **Gelişmiş yönetici paneli** — `/admin`: mod değiştirme, dil seçimi,
  başlat/durdur, istatistikler, sistem durumu, sohbet/grup kullanımı ve aktif
  işleri temizleme — hepsi butonlarla.
- **Premium emoji yönetimi** — kategorili panel, slot bazlı atama, toplu
  sıfırlama ve içe/dışa aktarma.
- **Hata bildirimleri** — indirme tamamlanamazsa yöneticiye özet, son 20 satır
  log ve hızlı mod değiştirme butonları gönderilir.
- **Local Bot API desteği** — opsiyonel; 50 MB üzeri dosya gönderimi için.

## Desteklenen Sistemler

Windows dışındaki tüm yaygın sistemlerde çalışır ve kurulum scripti bunları
otomatik algılar:

| Sistem | Otomatik başlatma |
|--------|-------------------|
| Debian / Ubuntu / Mint | systemd |
| Fedora / RHEL / Rocky / Alma | systemd |
| openSUSE | systemd |
| Arch / Manjaro | systemd |
| Alpine / Gentoo | OpenRC |
| FreeBSD | rc.d |
| macOS | launchd |

> **Gereksinimler:** Python 3.10+, `ffmpeg` (ses indirme için), `git`.
> `gallery-dl` ve `yt-dlp` kurulum sırasında sanal ortama yüklenir.

## Hızlı Kurulum

```bash
git clone <repo-url> downloader
cd downloader
bash install.sh
```

Kurulum scripti sırasıyla:

1. İşletim sistemini ve paket yöneticisini algılar.
2. Python, ffmpeg ve git'i kurar.
3. Sanal ortam (`venv`) oluşturup bağımlılıkları yükler.
4. Bot token, admin ID, bot adı gibi ayarları sorar.
5. İsteğe bağlı olarak start menüsü linklerini, Local Bot API'yi ve otomatik
   başlatmayı yapılandırır.

### Token ve Admin ID

- **Bot Token:** [@BotFather](https://t.me/BotFather) → `/newbot`
- **Admin ID:** [@userinfobot](https://t.me/userinfobot) size sayısal ID'nizi verir.

## Yapılandırma

Tüm ayarlar `data/.env` dosyasından okunur. Şablon için
[`data/example.env`](data/example.env) dosyasına bakın.

| Değişken | Açıklama | Varsayılan |
|----------|----------|------------|
| `BOT_TOKEN` | BotFather token'ı (zorunlu) | — |
| `ADMIN_ID` | Yönetici Telegram ID'si (zorunlu) | — |
| `BOT_NAME` | Botun görünen adı | `Downloader` |
| `SHOW_LINKS` | Start menüsünde linkleri göster | `false` |
| `OWNER_LINK` / `COMMUNITY_LINK` | Owner / topluluk linkleri | boş |
| `MAX_SIMULTANEOUS_DOWNLOADS` | Eş zamanlı indirme sayısı | `3` |
| `MAX_FILE_SIZE_MB` | Maksimum dosya boyutu | `1900` |
| `LOCAL_BOT_API_BASE` | Local Bot API adresi (opsiyonel) | boş |
| `CLEANUP_TZ_OFFSET` / `CLEANUP_HOUR` | Temizlik saati / dilimi | `-3` / `0` |
| `LOG_RETENTION_DAYS` | Log saklama süresi (gün) | `7` |
| `CACHE_ENABLED` | file_id önbelleği | `true` |
| `JOB_TIMEOUT_SEC` | Tek indirmenin üst süre sınırı (sn) | `1800` |
| `JOB_MAX_GB` | Tek indirmenin üst disk sınırı (GB) | `4` |
| `LIVE_STRIKE_LIMIT` | Kaçıncı canlı yayın denemesinde ban | `3` |
| `LIVE_BAN_DAYS` | Geçici ban süresi (gün) | `5` |
| `COBALT_API_URL` | Kendi cobalt instance adresin (opsiyonel) | boş |
| `COBALT_API_KEY` | cobalt API anahtarı (gerekiyorsa) | boş |
| `COBALT_TIMEOUT` | cobalt istek zaman aşımı (sn) | `30` |
| `DB_PATH` | SQLite veritabanı yolu | `data/bot.db` |

### İndirme kaynağı önceliği

`data/sources.json` platform bazında hangi kaynağın önce deneneceğini belirler.
Dosya elle düzenlenebilir; **bot yeniden başlatılmadan** yeni sıra devreye girer.

```json
{
  "default": ["ytdlp", "cobalt", "gallerydl"],
  "platforms": {
    "TikTok":    ["cobalt", "ytdlp", "gallerydl"],
    "YouTube":   ["ytdlp", "cobalt"]
  }
}
```

cobalt'ın herkese açık API'si **yoktur**; kullanmak için kendi instance'ını
çalıştırman gerekir. `COBALT_API_URL` boşsa cobalt sessizce atlanır ve bot
yt-dlp ile normal çalışır. Kurulum ve AGPL-3.0 lisans notu:
[`docs/COBALT.md`](docs/COBALT.md).

## Veritabanı

`data/bot.db` (SQLite) — kullanıcı ve sohbet kayıtları, indirme geçmişi.

| Tablo | İçerik |
|-------|--------|
| `users` | user_id, kullanıcı adı, ilk görülme, son aktivite, indirme sayısı, duyuru tercihi, engellenme durumu |
| `chats` | chat_id, başlık, tür, ilk görülme, son aktivite, indirme sayısı |
| `chat_platforms` | sohbet başına platform kullanım sayacı |
| `downloads` | her indirmenin tam geçmişi (platform, kaynak, boyut, süre, sonuç) |

Mevcut `chats.json` / `usage_stats.json` verisi bot ilk açılışta otomatik
taşınır (JSON dosyaları silinmez). Postgres'e geçiş için tüm SQL tek dosyada
(`bot/db.py`) toplanmıştır ve SQLite'a özel sözdizimi kullanılmaz.

## Komutlar

| Komut | Açıklama |
|-------|----------|
| `/start` | Karşılama menüsü |
| `/help` | Yardım ve desteklenen platformlar |
| `/ses <link>` | Bağlantıyı ses olarak indir |
| `/cancel` | Aktif indirmeyi iptal et |
| `/duyurular` | Bot duyurularını aç / kapat |
| `/admin` | (Yönetici) mod değiştirme ve kullanım paneli |
| `/status` | (Yönetici) sistem durumu |
| `/dur` · `/basla` | (Yönetici) botu durdur / başlat |
| `/banid` · `/unbanid` | (Yönetici) kullanıcı yasakla / kaldır |

## Çalışma Modları

- **normal** — olağan çalışma.
- **safe** — sessiz mod. Kullanıcıya hiçbir mesaj/bildirim gönderilmez; link
  sessizce indirilip yalnızca medya dosyası yanıt olarak iletilir.
- **maintenance** — bakım modu. İndirme yapılmaz; her isteğe sabit bir mesaj
  döner.

Mod `/admin` panelinden değiştirilir, `data/bot_state.json` içinde kalıcı tutulur
ve yeniden başlatmada korunur.

## Yönetici Paneli (`/admin`)

Tek bir buton arayüzünden tüm yönetim:

- **Mod** — Normal / Safe / Bakım arasında geçiş.
- **Başlat / Durdur** — botu tek dokunuşla duraklat veya çalıştır.
- **🌐 Dil** — kullanıcı mesajlarının dilini değiştir (anında uygulanır, kalıcı).
- **📊 İstatistik** — toplam/başarısız/iptal indirme ve platform dağılımı.
- **🖥 Sistem** — Local Bot API, aktif indirme, Python/yt-dlp/ffmpeg durumu.
- **💬 Kullanım** — botun kullanıldığı tüm sohbet ve gruplar (id, tür, indirme
  sayısı, son aktivite), sayfalı liste.
- **🧹 İşleri Temizle** — tüm aktif indirmeleri ve bekleyen menüleri iptal et.

## Dil

Bot, kullanıcıya gösterilen tüm metinleri (durum mesajları, butonlar, hata
açıklamaları, medya bilgileri) seçilen dilde gösterir. Desteklenen diller:
`tr`, `en`, `ru`, `de`, `es`, `fr`, `ar`. Varsayılan başlangıç dili
`data/.env` içindeki `BOT_NAME` ile aynı dosyadan değil, `/admin` panelinden
seçilir ve `data/bot_state.json`'da saklanır. Yönetici paneli Türkçedir.

## Premium Emoji Yönetimi

`/emojiler` veya panelden erişilen düzenleyici, mesajlardaki ikonları
[Telegram premium custom emoji](https://core.telegram.org/bots/api#messageentity)
ile değiştirmenizi sağlar:

- Slotlar kategorilere ayrılmıştır (menü, platform ikonları, bilgi alanları,
  butonlar, durum, owner).
- Bir premium emojiyi bota gönderin, ardından bir slota dokunarak atayın.
- Tek tek `♻️` ile veya **🗑 Tümünü Sıfırla** ile toplu sıfırlama.
- **📤 Dosya** ile `emoji_slots.json` yedeğini indirin.

> Premium emoji ataması zorunlu değildir; atanmayan slotlar standart emojiye düşer.

## Servis Yönetimi (systemd)

```bash
sudo systemctl status downloader-bot     # durum
sudo systemctl restart downloader-bot    # yeniden başlat
sudo systemctl stop downloader-bot       # durdur
journalctl -u downloader-bot -f          # canlı log
```

## Elle Çalıştırma

```bash
venv/bin/python start.py
```

## Proje Yapısı

```
bot/
  app.py            Uygulama kurulumu, olay döngüsü
  config.py         .env yapılandırması
  process_manager.py  Çoklu işlem indirme yöneticisi
  downloader/       yt-dlp / gallery-dl indirme mantığı
  handlers/         Komut, mesaj ve buton işleyicileri
  sender.py         Telegram'a medya gönderimi + önbellek
  cache.py          file_id önbelleği
  chats.py          Kullanım istatistikleri
  state.py          Çalışma modu yönetimi
  scheduler.py      Günlük temizlik
  ui.py             Mesaj ve klavye şablonları
start.py            Giriş noktası
install.sh          Otomatik kurulum
```

## Notlar

- 50 MB üzeri dosya göndermek için bir
  [Local Bot API](https://github.com/tdlib/telegram-bot-api) sunucusu gerekir;
  `LOCAL_BOT_API_BASE` ile etkinleştirilir.
- Giriş gerektiren içerikler için `data/cookies.txt` (Netscape formatı) ekleyin.

## Lisans

MIT
