# cobalt entegrasyonu

## Neden

cobalt bazı platformlarda yt-dlp'den belirgin şekilde hızlı. Bu VDS'te ölçülen
gerçek değerler:

| Platform  | cobalt | yt-dlp | Sonuç |
| --------- | -----: | -----: | ----- |
| TikTok    | 1.2 sn | 5.2 sn | cobalt ~4x hızlı |
| YouTube   | çalışmıyor (bkz. aşağıda) | 13.7 sn | yt-dlp |
| Instagram | çalışmıyor (giriş gerekiyor) | 4.7 sn | yt-dlp |

cobalt sunucu tarafında hazır çözümleme yapar; TikTok/Twitter gibi sık HTML
değiştiren platformlarda daha stabil kalır ve watermark'sız sonuç verir.

## Herkese açık API YOK

cobalt'ın resmî ortak API'si kapalı:

> "there is currently no publicly available pre-hosted api"
> — [api/README.md](https://github.com/imputnet/cobalt/blob/main/api/README.md)

Yani kullanmak için **kendi instance'ını çalıştırman gerekiyor**.
`COBALT_API_URL` boş bırakılırsa cobalt kaynağı sessizce atlanır ve bot
yalnızca yt-dlp + gallery-dl ile çalışır. Yani bu entegrasyon opsiyoneldir,
bot cobalt olmadan da tam çalışır.

## Kurulum

```bash
docker run -d --name cobalt --restart unless-stopped \
  -p 127.0.0.1:9000:9000 \
  -e API_URL="http://127.0.0.1:9000/" \
  --memory=1g --cpus=1 \
  ghcr.io/imputnet/cobalt:11
```

Sonra `data/.env` içine:

```env
COBALT_API_URL=http://127.0.0.1:9000
# COBALT_API_KEY=...    # instance'ı kimlik doğrulamalı çalıştırıyorsan
COBALT_TIMEOUT=30
```

**Portu dışarı açma.** `127.0.0.1:9000` bağlaması bilinçli: cobalt'ı internete
açarsan hem AGPL yükümlülüğü doğar (aşağı bak) hem de açık bir indirme proxy'si
işletmiş olursun.

## YouTube neden çalışmıyor

cobalt, YouTube'u veri merkezi IP'sinden çekerken YouTube tarafından
engelleniyor; tünel HTTP 200 ama **0 bayt** dönüyor. Çözümü ayrı bir
`YOUTUBE_SESSION_SERVER` (po_token üreteci) çalıştırmak — ek karmaşıklık.

Bu yüzden varsayılan öncelikte YouTube/YouTube Music için **yt-dlp önce**
geliyor. Ayrıca istemci 0 baytlık sonucu hata sayıp sıradaki kaynağa geçiyor,
yani session server kurmasan bile kullanıcıya boş dosya gitmez.

## Öncelik sırası

`data/sources.json` ile yönetilir, **kod değiştirmeden** düzenlenebilir
(dosya değişince bot yeniden başlatılmadan yeni sıra devreye girer):

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

Bir kaynak hata verirse sıradaki denenir. cobalt yapılandırılmamışsa veya
platformu desteklemiyorsa listeden otomatik düşer.

## Lisans — AGPL-3.0 (önemli)

cobalt **AGPL-3.0** lisanslı. Bu entegrasyonun lisans durumu:

**Bu bot AGPL'e tabi değil.** `bot/downloader/cobalt.py` cobalt kaynak kodunu
içermez, kopyalamaz veya link'lemez; yalnızca HTTP üzerinden ayrı bir servise
istek atar. Ağ üzerinden ayrı bir programla haberleşmek türev eser (derivative
work) oluşturmaz.

**Ama cobalt instance'ını sen barındırıyorsan yükümlülüğün var.** AGPL-3.0
§13, yazılımı ağ üzerinden kullanıcılara sunan herkesin kaynak kodu o
kullanıcılara sunmasını şart koşar:

> "if you modify the Program, your modified version must prominently offer all
> users interacting with it remotely through a computer network ... an
> opportunity to receive the Corresponding Source of your version"

Pratikte:

1. **cobalt'ı değiştirmeden resmî imajla çalıştırıyorsan** (yukarıdaki docker
   komutu), kaynak kod zaten upstream'de herkese açık. Ek yükümlülük pratikte
   yok; yine de dürüstlük için botun `/help` veya `/start` metninde cobalt
   kullanıldığını ve upstream linkini belirtmek doğru olur.
2. **cobalt'ta değişiklik yaparsan** (fork, yama, servis ekleme), değiştirilmiş
   kaynağı bot kullanıcılarına açmak **zorundasın**.
3. Botun kendi kaynak kodu bundan etkilenmez — istediğin lisansta kalabilir.

Riski sıfırlamak istersen: cobalt'ı değiştirme, resmî imajı kullan.
