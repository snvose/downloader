from __future__ import annotations

"""
Multi-language user-facing text.

The bot language is a single global setting, chosen by the admin from the
/admin panel and persisted in data/bot_state.json. t(key, **fmt) returns the
text for the current language, falling back to English and then to the key
itself.
"""

LANGUAGES: dict[str, str] = {
    "en": "English",
    "tr": "Türkçe",
    "ru": "Русский",
    "de": "Deutsch",
    "es": "Español",
    "fr": "Français",
    "ar": "العربية",
}

DEFAULT_LANG = "en"
_current = DEFAULT_LANG


def set_language(lang: str) -> str:
    global _current
    _current = lang if lang in LANGUAGES else DEFAULT_LANG
    return _current


def get_language() -> str:
    return _current


def t(key: str, **fmt) -> str:
    table = _STRINGS.get(key, {})
    text = table.get(_current) or table.get("en") or table.get("tr") or key
    if fmt:
        try:
            return text.format(**fmt)
        except Exception:
            return text
    return text


# ── Translation table ────────────────────────────────────────────────────────
# Each key: { lang_code: text }. Placeholders use {name} format syntax.
_STRINGS: dict[str, dict[str, str]] = {
    # ── Status messages ──
    "analyzing": {
        "tr": "Link analiz ediliyor...", "en": "Analyzing link...",
        "ru": "Анализ ссылки...", "de": "Link wird analysiert...",
        "es": "Analizando enlace...", "fr": "Analyse du lien...",
        "ar": "جارٍ تحليل الرابط...",
    },
    "preparing": {
        "tr": "Hazırlanıyor...", "en": "Preparing...",
        "ru": "Подготовка...", "de": "Wird vorbereitet...",
        "es": "Preparando...", "fr": "Préparation...",
        "ar": "جارٍ التحضير...",
    },
    "downloading": {
        "tr": "İndiriliyor", "en": "Downloading",
        "ru": "Загрузка", "de": "Wird heruntergeladen",
        "es": "Descargando", "fr": "Téléchargement",
        "ar": "جارٍ التنزيل",
    },
    "processing": {
        "tr": "Son işlemler yapılıyor...", "en": "Finishing up...",
        "ru": "Завершение обработки...", "de": "Wird abgeschlossen...",
        "es": "Finalizando...", "fr": "Finalisation...",
        "ar": "جارٍ الإنهاء...",
    },
    "uploading": {
        "tr": "Telegram'a yükleniyor...", "en": "Uploading to Telegram...",
        "ru": "Загрузка в Telegram...", "de": "Wird zu Telegram hochgeladen...",
        "es": "Subiendo a Telegram...", "fr": "Envoi vers Telegram...",
        "ar": "جارٍ الرفع إلى تيليجرام...",
    },
    "cancelled": {
        "tr": "İndirme iptal edildi.", "en": "Download cancelled.",
        "ru": "Загрузка отменена.", "de": "Download abgebrochen.",
        "es": "Descarga cancelada.", "fr": "Téléchargement annulé.",
        "ar": "تم إلغاء التنزيل.",
    },
    "audio_preparing": {
        "tr": "Ses hazırlanıyor...", "en": "Preparing audio...",
        "ru": "Подготовка аудио...", "de": "Audio wird vorbereitet...",
        "es": "Preparando audio...", "fr": "Préparation de l'audio...",
        "ar": "جارٍ تحضير الصوت...",
    },
    "spotify_preparing": {
        "tr": "Spotify şarkısı hazırlanıyor...", "en": "Preparing Spotify track...",
        "ru": "Подготовка трека Spotify...", "de": "Spotify-Titel wird vorbereitet...",
        "es": "Preparando canción de Spotify...", "fr": "Préparation du titre Spotify...",
        "ar": "جارٍ تحضير أغنية Spotify...",
    },

    # ── /start and /help ──
    "start_desc": {
        "tr": "Medya bağlantılarını hızlı ve sade şekilde indirmek için hazırlanmıştır.\nLink gönder, bot içeriği hazırlayıp sohbete yüklesin.",
        "en": "Download media links quickly and cleanly.\nSend a link and the bot will fetch and upload it.",
        "ru": "Быстрая и простая загрузка медиа по ссылке.\nОтправьте ссылку — бот скачает и загрузит контент.",
        "de": "Lade Medien-Links schnell und unkompliziert herunter.\nSende einen Link, der Bot lädt ihn herunter.",
        "es": "Descarga enlaces de medios de forma rápida y sencilla.\nEnvía un enlace y el bot lo subirá.",
        "fr": "Téléchargez des liens multimédias simplement et rapidement.\nEnvoyez un lien, le bot s'en occupe.",
        "ar": "نزّل روابط الوسائط بسرعة وببساطة.\nأرسل رابطًا وسيقوم البوت بتنزيله ورفعه.",
    },
    "help_title": {
        "tr": "Yardım", "en": "Help", "ru": "Помощь", "de": "Hilfe",
        "es": "Ayuda", "fr": "Aide", "ar": "مساعدة",
    },
    "help_platforms": {
        "tr": "Desteklenen platformlar", "en": "Supported platforms",
        "ru": "Поддерживаемые платформы", "de": "Unterstützte Plattformen",
        "es": "Plataformas compatibles", "fr": "Plateformes prises en charge",
        "ar": "المنصات المدعومة",
    },
    "help_broadcasts": {
        "tr": "bot duyurularını açar / kapatır.",
        "en": "turn bot announcements on / off.",
        "ru": "включить / выключить объявления бота.",
        "de": "Bot-Ankündigungen ein-/ausschalten.",
        "es": "activa / desactiva los anuncios del bot.",
        "fr": "active / désactive les annonces du bot.",
        "ar": "تفعيل / إيقاف إعلانات البوت.",
    },
    "help_no_live": {
        "tr": "Canlı yayınlar desteklenmez — yayın bittikten sonra kaydını gönderebilirsin.",
        "en": "Live streams are not supported — send the recording once the stream ends.",
        "ru": "Прямые трансляции не поддерживаются — отправьте запись после эфира.",
        "de": "Livestreams werden nicht unterstützt — sende die Aufzeichnung nach dem Stream.",
        "es": "Las transmisiones en vivo no son compatibles — envía la grabación al terminar.",
        "fr": "Les directs ne sont pas pris en charge — envoyez l'enregistrement après.",
        "ar": "البث المباشر غير مدعوم — أرسل التسجيل بعد انتهاء البث.",
    },
    "help_commands": {
        "tr": "Komutlar", "en": "Commands", "ru": "Команды", "de": "Befehle",
        "es": "Comandos", "fr": "Commandes", "ar": "الأوامر",
    },
    "help_ses": {
        "tr": "bağlantıyı ses olarak indirir.", "en": "download the link as audio.",
        "ru": "скачать ссылку как аудио.", "de": "Link als Audio herunterladen.",
        "es": "descarga el enlace como audio.", "fr": "télécharge le lien en audio.",
        "ar": "تنزيل الرابط كملف صوتي.",
    },
    "help_cancel": {
        "tr": "aktif indirmeyi iptal eder.", "en": "cancel the active download.",
        "ru": "отменить текущую загрузку.", "de": "aktiven Download abbrechen.",
        "es": "cancela la descarga activa.", "fr": "annule le téléchargement en cours.",
        "ar": "إلغاء التنزيل الحالي.",
    },
    "help_spotify": {
        "tr": "Spotify yalnızca tekil şarkı olarak desteklenir; albüm/playlist indirilemez.",
        "en": "Spotify is supported for single tracks only; albums/playlists cannot be downloaded.",
        "ru": "Spotify поддерживается только для отдельных треков; альбомы/плейлисты недоступны.",
        "de": "Spotify wird nur für einzelne Titel unterstützt; Alben/Playlists nicht.",
        "es": "Spotify solo admite canciones individuales; no álbumes/listas.",
        "fr": "Spotify n'est pris en charge que pour les titres uniques ; pas les albums/playlists.",
        "ar": "يُدعم Spotify للأغاني الفردية فقط؛ لا يمكن تنزيل الألبومات/قوائم التشغيل.",
    },
    "spotify_unsupported": {
        "tr": "Spotify albüm / playlist desteklenmiyor. Yalnızca tekil şarkı (track) linkleri indirilebilir.",
        "en": "Spotify albums / playlists are not supported. Only single track links can be downloaded.",
        "ru": "Альбомы/плейлисты Spotify не поддерживаются. Доступны только ссылки на отдельные треки.",
        "de": "Spotify-Alben/Playlists werden nicht unterstützt. Nur einzelne Titel-Links.",
        "es": "No se admiten álbumes/listas de Spotify. Solo enlaces de canciones individuales.",
        "fr": "Les albums/playlists Spotify ne sont pas pris en charge. Seuls les titres uniques.",
        "ar": "ألبومات/قوائم تشغيل Spotify غير مدعومة. روابط الأغاني الفردية فقط.",
    },

    # ── Buttons ──
    "btn_details": {
        "tr": "Detaylar", "en": "Details", "ru": "Подробнее", "de": "Details",
        "es": "Detalles", "fr": "Détails", "ar": "التفاصيل",
    },
    "btn_source": {
        "tr": "Kaynak", "en": "Source", "ru": "Источник", "de": "Quelle",
        "es": "Fuente", "fr": "Source", "ar": "المصدر",
    },

    # ── Media info fields ──
    "info_suffix": {
        "tr": "Bilgileri", "en": "Info", "ru": "Информация", "de": "Infos",
        "es": "Información", "fr": "Infos", "ar": "معلومات",
    },
    "f_title": {
        "tr": "Başlık", "en": "Title", "ru": "Название", "de": "Titel",
        "es": "Título", "fr": "Titre", "ar": "العنوان",
    },
    "f_uploader": {
        "tr": "Profil/Kanal", "en": "Profile/Channel", "ru": "Профиль/Канал",
        "de": "Profil/Kanal", "es": "Perfil/Canal", "fr": "Profil/Chaîne",
        "ar": "الحساب/القناة",
    },
    "f_uploader_id": {
        "tr": "Profil ID", "en": "Profile ID", "ru": "ID профиля",
        "de": "Profil-ID", "es": "ID de perfil", "fr": "ID du profil",
        "ar": "معرّف الحساب",
    },
    "f_duration": {
        "tr": "Süre", "en": "Duration", "ru": "Длительность", "de": "Dauer",
        "es": "Duración", "fr": "Durée", "ar": "المدة",
    },
    "f_quality": {
        "tr": "Kalite", "en": "Quality", "ru": "Качество", "de": "Qualität",
        "es": "Calidad", "fr": "Qualité", "ar": "الجودة",
    },
    "f_format": {
        "tr": "Format", "en": "Format", "ru": "Формат", "de": "Format",
        "es": "Formato", "fr": "Format", "ar": "الصيغة",
    },
    "f_size": {
        "tr": "Boyut", "en": "Size", "ru": "Размер", "de": "Größe",
        "es": "Tamaño", "fr": "Taille", "ar": "الحجم",
    },
    "f_views": {
        "tr": "İzlenme", "en": "Views", "ru": "Просмотры", "de": "Aufrufe",
        "es": "Vistas", "fr": "Vues", "ar": "المشاهدات",
    },
    "f_likes": {
        "tr": "Beğeni", "en": "Likes", "ru": "Лайки", "de": "Likes",
        "es": "Me gusta", "fr": "J'aime", "ar": "الإعجابات",
    },
    "f_description": {
        "tr": "Açıklama", "en": "Description", "ru": "Описание", "de": "Beschreibung",
        "es": "Descripción", "fr": "Description", "ar": "الوصف",
    },
    "unknown": {
        "tr": "Bilinmiyor", "en": "Unknown", "ru": "Неизвестно", "de": "Unbekannt",
        "es": "Desconocido", "fr": "Inconnu", "ar": "غير معروف",
    },

    # ── Broadcast opt in/out ──
    "broadcast_opt_out": {
        "tr": "🔕 <b>Duyurular kapatıldı.</b>\nArtık bot duyurularını almayacaksın. Tekrar açmak için yine /broadcasts yaz.",
        "en": "🔕 <b>Announcements turned off.</b>\nYou will no longer receive bot announcements. Send /broadcasts again to turn them back on.",
        "ru": "🔕 <b>Объявления отключены.</b>\nВы больше не будете получать объявления. Отправьте /broadcasts снова, чтобы включить.",
        "de": "🔕 <b>Ankündigungen deaktiviert.</b>\nDu erhältst keine Bot-Ankündigungen mehr. Sende /broadcasts erneut zum Aktivieren.",
        "es": "🔕 <b>Anuncios desactivados.</b>\nYa no recibirás anuncios del bot. Envía /broadcasts de nuevo para reactivarlos.",
        "fr": "🔕 <b>Annonces désactivées.</b>\nVous ne recevrez plus d'annonces. Renvoyez /broadcasts pour les réactiver.",
        "ar": "🔕 <b>تم إيقاف الإعلانات.</b>\nلن تتلقى إعلانات البوت بعد الآن. أرسل /broadcasts مرة أخرى لتفعيلها.",
    },
    "broadcast_opt_in": {
        "tr": "🔔 <b>Duyurular açıldı.</b>\nBot duyurularını alacaksın. Kapatmak için /broadcasts yaz.",
        "en": "🔔 <b>Announcements turned on.</b>\nYou will receive bot announcements. Send /broadcasts to turn them off.",
        "ru": "🔔 <b>Объявления включены.</b>\nВы будете получать объявления. Отправьте /broadcasts, чтобы отключить.",
        "de": "🔔 <b>Ankündigungen aktiviert.</b>\nDu erhältst Bot-Ankündigungen. Sende /broadcasts zum Deaktivieren.",
        "es": "🔔 <b>Anuncios activados.</b>\nRecibirás anuncios del bot. Envía /broadcasts para desactivarlos.",
        "fr": "🔔 <b>Annonces activées.</b>\nVous recevrez les annonces. Envoyez /broadcasts pour les désactiver.",
        "ar": "🔔 <b>تم تفعيل الإعلانات.</b>\nستتلقى إعلانات البوت. أرسل /broadcasts لإيقافها.",
    },

    # ── Livestream protection ──
    "live_not_supported": {
        "tr": "🔴 <b>Canlı yayınlar desteklenmiyor.</b>\nCanlı yayınların sonu olmadığı için indirilemez. Yayın bittikten sonra kaydını gönderebilirsin.",
        "en": "🔴 <b>Live streams are not supported.</b>\nA live stream never ends, so it cannot be downloaded. Send the recording once the stream is over.",
        "ru": "🔴 <b>Прямые трансляции не поддерживаются.</b>\nТрансляция не имеет конца, поэтому её нельзя скачать. Отправьте запись после окончания эфира.",
        "de": "🔴 <b>Livestreams werden nicht unterstützt.</b>\nEin Livestream endet nie und kann daher nicht heruntergeladen werden. Sende die Aufzeichnung, sobald der Stream vorbei ist.",
        "es": "🔴 <b>Las transmisiones en vivo no son compatibles.</b>\nUna transmisión en vivo no termina, por lo que no se puede descargar. Envía la grabación cuando el directo haya terminado.",
        "fr": "🔴 <b>Les directs ne sont pas pris en charge.</b>\nUn direct n'a pas de fin, il ne peut donc pas être téléchargé. Envoyez l'enregistrement une fois le direct terminé.",
        "ar": "🔴 <b>البث المباشر غير مدعوم.</b>\nالبث المباشر لا ينتهي، لذا لا يمكن تنزيله. أرسل التسجيل بعد انتهاء البث.",
    },
    "live_last_warning": {
        "tr": "🔴 <b>Canlı yayınlar desteklenmiyor.</b>\n⚠️ <b>Son uyarı:</b> bir kez daha canlı yayın linki gönderirsen geçici olarak banlanacaksın.",
        "en": "🔴 <b>Live streams are not supported.</b>\n⚠️ <b>Final warning:</b> send one more live link and you will be temporarily banned.",
        "ru": "🔴 <b>Прямые трансляции не поддерживаются.</b>\n⚠️ <b>Последнее предупреждение:</b> ещё одна такая ссылка — и вы будете временно заблокированы.",
        "de": "🔴 <b>Livestreams werden nicht unterstützt.</b>\n⚠️ <b>Letzte Warnung:</b> Noch ein Live-Link und du wirst vorübergehend gesperrt.",
        "es": "🔴 <b>Las transmisiones en vivo no son compatibles.</b>\n⚠️ <b>Última advertencia:</b> un enlace en vivo más y serás baneado temporalmente.",
        "fr": "🔴 <b>Les directs ne sont pas pris en charge.</b>\n⚠️ <b>Dernier avertissement :</b> encore un lien en direct et vous serez temporairement banni.",
        "ar": "🔴 <b>البث المباشر غير مدعوم.</b>\n⚠️ <b>تحذير أخير:</b> رابط بث مباشر آخر وسيتم حظرك مؤقتًا.",
    },
    "live_temp_banned": {
        "tr": "🚫 <b>{days} gün boyunca banlısınız.</b>\nTekrar tekrar canlı yayın linki gönderdiğiniz için botu geçici olarak kullanamazsınız. Süre dolunca ban otomatik kalkacak.",
        "en": "🚫 <b>You are banned for {days} days.</b>\nYou repeatedly sent live stream links, so bot access is temporarily suspended. The ban lifts automatically when the time is up.",
        "ru": "🚫 <b>Вы заблокированы на {days} дней.</b>\nВы неоднократно отправляли ссылки на трансляции, поэтому доступ временно приостановлен. Блокировка снимется автоматически.",
        "de": "🚫 <b>Du bist für {days} Tage gesperrt.</b>\nDu hast wiederholt Livestream-Links gesendet, daher ist der Zugang vorübergehend gesperrt. Die Sperre endet automatisch.",
        "es": "🚫 <b>Estás baneado durante {days} días.</b>\nEnviaste enlaces de directos repetidamente, así que el acceso queda suspendido temporalmente. El baneo se levanta solo.",
        "fr": "🚫 <b>Vous êtes banni pendant {days} jours.</b>\nVous avez envoyé des liens de direct à répétition, l'accès est donc suspendu temporairement. Le bannissement sera levé automatiquement.",
        "ar": "🚫 <b>أنت محظور لمدة {days} أيام.</b>\nلقد أرسلت روابط بث مباشر بشكل متكرر، لذا تم تعليق الوصول مؤقتًا. سيُرفع الحظر تلقائيًا.",
    },
    "live_ban_active": {
        "tr": "🚫 <b>Banlısınız.</b>\nKalan süre: <b>{duration}</b>. Süre dolunca ban otomatik kalkacak.",
        "en": "🚫 <b>You are banned.</b>\nTime remaining: <b>{duration}</b>. The ban lifts automatically.",
        "ru": "🚫 <b>Вы заблокированы.</b>\nОсталось: <b>{duration}</b>. Блокировка снимется автоматически.",
        "de": "🚫 <b>Du bist gesperrt.</b>\nVerbleibende Zeit: <b>{duration}</b>. Die Sperre endet automatisch.",
        "es": "🚫 <b>Estás baneado.</b>\nTiempo restante: <b>{duration}</b>. El baneo se levanta solo.",
        "fr": "🚫 <b>Vous êtes banni.</b>\nTemps restant : <b>{duration}</b>. Le bannissement sera levé automatiquement.",
        "ar": "🚫 <b>أنت محظور.</b>\nالوقت المتبقي: <b>{duration}</b>. سيُرفع الحظر تلقائيًا.",
    },
    "job_timeout": {
        "tr": "⏱ <b>İndirme zaman aşımına uğradı.</b>\nİşlem çok uzun sürdüğü için durduruldu. İçerik çok büyük veya kaynak yavaş olabilir.",
        "en": "⏱ <b>Download timed out.</b>\nIt was stopped for taking too long. The content may be too large or the source too slow.",
        "ru": "⏱ <b>Время загрузки истекло.</b>\nОперация остановлена из-за большой длительности. Контент слишком большой или источник медленный.",
        "de": "⏱ <b>Zeitüberschreitung beim Download.</b>\nDer Vorgang wurde abgebrochen, weil er zu lange dauerte.",
        "es": "⏱ <b>La descarga expiró.</b>\nSe detuvo por tardar demasiado. El contenido puede ser muy grande o la fuente lenta.",
        "fr": "⏱ <b>Délai de téléchargement dépassé.</b>\nL'opération a été arrêtée car trop longue.",
        "ar": "⏱ <b>انتهت مهلة التنزيل.</b>\nتم إيقاف العملية لأنها استغرقت وقتًا طويلاً.",
    },
    "job_oversize": {
        "tr": "📦 <b>İçerik çok büyük.</b>\nBu indirme izin verilen disk sınırını aştığı için durduruldu.",
        "en": "📦 <b>Content too large.</b>\nThis download was stopped for exceeding the allowed size limit.",
        "ru": "📦 <b>Контент слишком большой.</b>\nЗагрузка остановлена из-за превышения лимита размера.",
        "de": "📦 <b>Inhalt zu groß.</b>\nDieser Download wurde wegen Überschreitung des Größenlimits gestoppt.",
        "es": "📦 <b>Contenido demasiado grande.</b>\nLa descarga se detuvo por superar el límite de tamaño.",
        "fr": "📦 <b>Contenu trop volumineux.</b>\nCe téléchargement a été arrêté car il dépasse la limite de taille.",
        "ar": "📦 <b>المحتوى كبير جدًا.</b>\nتم إيقاف التنزيل لتجاوزه حد الحجم المسموح.",
    },
    "job_failed_generic": {
        "tr": "❌ <b>İndirme tamamlanamadı.</b>\nİşlem beklenmedik şekilde sonlandı. Lütfen tekrar dene.",
        "en": "❌ <b>Download failed.</b>\nThe process ended unexpectedly. Please try again.",
        "ru": "❌ <b>Загрузка не удалась.</b>\nПроцесс неожиданно завершился. Попробуйте ещё раз.",
        "de": "❌ <b>Download fehlgeschlagen.</b>\nDer Vorgang wurde unerwartet beendet. Bitte erneut versuchen.",
        "es": "❌ <b>Descarga fallida.</b>\nEl proceso terminó inesperadamente. Inténtalo de nuevo.",
        "fr": "❌ <b>Échec du téléchargement.</b>\nLe processus s'est terminé de manière inattendue. Réessayez.",
        "ar": "❌ <b>فشل التنزيل.</b>\nانتهت العملية بشكل غير متوقع. حاول مرة أخرى.",
    },

    # ── Warning / error messages ──
    "wait_active": {
        "tr": "Önce aktif indirmenin bitmesini bekle veya /cancel yaz.",
        "en": "Wait for the active download to finish, or send /cancel.",
        "ru": "Дождитесь завершения текущей загрузки или отправьте /cancel.",
        "de": "Warte, bis der aktive Download fertig ist, oder sende /cancel.",
        "es": "Espera a que termine la descarga actual o envía /cancel.",
        "fr": "Attendez la fin du téléchargement en cours ou envoyez /cancel.",
        "ar": "انتظر انتهاء التنزيل الحالي أو أرسل /cancel.",
    },
    "cancel_done": {
        "tr": "Aktif indirme iptal edildi.", "en": "Active download cancelled.",
        "ru": "Текущая загрузка отменена.", "de": "Aktiver Download abgebrochen.",
        "es": "Descarga activa cancelada.", "fr": "Téléchargement en cours annulé.",
        "ar": "تم إلغاء التنزيل الحالي.",
    },
    "cancel_none": {
        "tr": "Aktif indirmen yok.", "en": "You have no active download.",
        "ru": "У вас нет активных загрузок.", "de": "Du hast keinen aktiven Download.",
        "es": "No tienes ninguna descarga activa.", "fr": "Aucun téléchargement en cours.",
        "ar": "ليس لديك تنزيل نشط.",
    },
    "details_sent": {
        "tr": "Detaylar zaten gönderildi.", "en": "Details already sent.",
        "ru": "Подробности уже отправлены.", "de": "Details wurden bereits gesendet.",
        "es": "Los detalles ya se enviaron.", "fr": "Les détails ont déjà été envoyés.",
        "ar": "تم إرسال التفاصيل بالفعل.",
    },
    "details_none": {
        "tr": "Detay bulunamadı.", "en": "No details found.",
        "ru": "Подробности не найдены.", "de": "Keine Details gefunden.",
        "es": "No se encontraron detalles.", "fr": "Aucun détail trouvé.",
        "ar": "لم يتم العثور على تفاصيل.",
    },
    "menu_expired": {
        "tr": "Bu içerik menüsünün süresi dolmuş.", "en": "This content menu has expired.",
        "ru": "Срок действия этого меню истёк.", "de": "Dieses Menü ist abgelaufen.",
        "es": "Este menú ha caducado.", "fr": "Ce menu a expiré.",
        "ar": "انتهت صلاحية هذه القائمة.",
    },
    "menu_expired_link": {
        "tr": "Bu menünün süresi dolmuş. Yeni link gönder.",
        "en": "This menu has expired. Send a new link.",
        "ru": "Срок действия меню истёк. Отправьте новую ссылку.",
        "de": "Dieses Menü ist abgelaufen. Sende einen neuen Link.",
        "es": "Este menú ha caducado. Envía un nuevo enlace.",
        "fr": "Ce menu a expiré. Envoyez un nouveau lien.",
        "ar": "انتهت صلاحية هذه القائمة. أرسل رابطًا جديدًا.",
    },
    "menu_not_yours": {
        "tr": "Bu menü sana ait değil.", "en": "This menu isn't yours.",
        "ru": "Это меню не ваше.", "de": "Dieses Menü gehört dir nicht.",
        "es": "Este menú no es tuyo.", "fr": "Ce menu n'est pas le vôtre.",
        "ar": "هذه القائمة ليست لك.",
    },
    "maintenance": {
        "tr": "Bot şu an bakımda, lütfen beklemede kalın.",
        "en": "The bot is under maintenance, please stand by.",
        "ru": "Бот на техническом обслуживании, пожалуйста, подождите.",
        "de": "Der Bot wird gewartet, bitte habe etwas Geduld.",
        "es": "El bot está en mantenimiento, por favor espera.",
        "fr": "Le bot est en maintenance, veuillez patienter.",
        "ar": "البوت قيد الصيانة، يُرجى الانتظار.",
    },
    "upload_failed": {
        "tr": "Dosyalar Telegram'a yüklenemedi. Sunucu veya Telegram API hatası olabilir.",
        "en": "Files couldn't be uploaded to Telegram. Server or Telegram API error.",
        "ru": "Не удалось загрузить файлы в Telegram. Ошибка сервера или Telegram API.",
        "de": "Dateien konnten nicht zu Telegram hochgeladen werden. Server- oder API-Fehler.",
        "es": "No se pudieron subir los archivos a Telegram. Error del servidor o de la API.",
        "fr": "Impossible d'envoyer les fichiers à Telegram. Erreur serveur ou API.",
        "ar": "تعذّر رفع الملفات إلى تيليجرام. خطأ في الخادم أو واجهة تيليجرام.",
    },
    "flood_limit": {
        "tr": "⏳ Telegram şu anda çok yoğun (flood limiti). Lütfen birkaç dakika sonra tekrar deneyin.",
        "en": "⏳ Telegram is rate-limiting the bot right now. Please try again in a few minutes.",
        "ru": "⏳ Telegram сейчас ограничивает частоту запросов. Повторите через несколько минут.",
        "de": "⏳ Telegram limitiert den Bot gerade. Bitte versuche es in ein paar Minuten erneut.",
        "es": "⏳ Telegram está limitando la frecuencia ahora mismo. Inténtalo de nuevo en unos minutos.",
        "fr": "⏳ Telegram limite actuellement la fréquence des messages. Réessayez dans quelques minutes.",
        "ar": "⏳ يقوم تيليجرام حاليًا بتقييد المعدل. يُرجى المحاولة مرة أخرى بعد بضع دقائق.",
    },
    "ses_usage": {
        "tr": "Kullanım: <code>/audio link</code>\nLinke cevap olarak sadece <code>/audio</code> de yazabilirsin.",
        "en": "Usage: <code>/audio link</code>\nYou can also reply <code>/audio</code> to a link.",
        "ru": "Использование: <code>/audio ссылка</code>\nМожно также ответить <code>/audio</code> на ссылку.",
        "de": "Verwendung: <code>/audio Link</code>\nDu kannst auch mit <code>/audio</code> auf einen Link antworten.",
        "es": "Uso: <code>/audio enlace</code>\nTambién puedes responder <code>/audio</code> a un enlace.",
        "fr": "Usage : <code>/audio lien</code>\nVous pouvez aussi répondre <code>/audio</code> à un lien.",
        "ar": "الاستخدام: <code>/audio رابط</code>\nيمكنك أيضًا الرد بـ <code>/audio</code> على رابط.",
    },
    "too_large": {
        "tr": "Dosya gönderilemedi.\n\nDosya: {name}\nBoyut: {size}\nSebep: {reason}.",
        "en": "File couldn't be sent.\n\nFile: {name}\nSize: {size}\nReason: {reason}.",
        "ru": "Не удалось отправить файл.\n\nФайл: {name}\nРазмер: {size}\nПричина: {reason}.",
        "de": "Datei konnte nicht gesendet werden.\n\nDatei: {name}\nGröße: {size}\nGrund: {reason}.",
        "es": "No se pudo enviar el archivo.\n\nArchivo: {name}\nTamaño: {size}\nMotivo: {reason}.",
        "fr": "Impossible d'envoyer le fichier.\n\nFichier : {name}\nTaille : {size}\nRaison : {reason}.",
        "ar": "تعذّر إرسال الملف.\n\nالملف: {name}\nالحجم: {size}\nالسبب: {reason}.",
    },
    "job_start_failed": {
        "tr": "İş başlatılamadı.", "en": "Couldn't start the job.",
        "ru": "Не удалось запустить задачу.", "de": "Vorgang konnte nicht gestartet werden.",
        "es": "No se pudo iniciar la tarea.", "fr": "Impossible de démarrer la tâche.",
        "ar": "تعذّر بدء المهمة.",
    },
    "playlist_cancelled": {
        "tr": "Playlist iptal edildi.", "en": "Playlist cancelled.",
        "ru": "Плейлист отменён.", "de": "Playlist abgebrochen.",
        "es": "Lista de reproducción cancelada.", "fr": "Playlist annulée.",
        "ar": "تم إلغاء قائمة التشغيل.",
    },
    "banned_user": {
        "tr": "Bu kullanıcı botu kullanamaz.", "en": "This user cannot use the bot.",
        "ru": "Этот пользователь не может использовать бота.", "de": "Dieser Nutzer kann den Bot nicht verwenden.",
        "es": "Este usuario no puede usar el bot.", "fr": "Cet utilisateur ne peut pas utiliser le bot.",
        "ar": "لا يمكن لهذا المستخدم استخدام البوت.",
    },
    "banned_group": {
        "tr": "Bot bu grupta kullanılamıyor.", "en": "The bot cannot be used in this group.",
        "ru": "Бота нельзя использовать в этой группе.", "de": "Der Bot kann in dieser Gruppe nicht verwendet werden.",
        "es": "El bot no se puede usar en este grupo.", "fr": "Le bot ne peut pas être utilisé dans ce groupe.",
        "ar": "لا يمكن استخدام البوت في هذه المجموعة.",
    },
    "unit_days": {
        "tr": "gün", "en": "days", "ru": "дн.", "de": "Tage",
        "es": "días", "fr": "jours", "ar": "أيام",
    },
    "unit_hours": {
        "tr": "saat", "en": "hours", "ru": "ч.", "de": "Stunden",
        "es": "horas", "fr": "heures", "ar": "ساعات",
    },
    "unit_minutes": {
        "tr": "dakika", "en": "minutes", "ru": "мин.", "de": "Minuten",
        "es": "minutos", "fr": "minutes", "ar": "دقائق",
    },

    # ── safe_public_error mappings ──
    "err_tiktok_403": {
        "tr": "TikTok bu bağlantıya erişim engeli verdi. Farklı yöntemler denendi ama erişilemedi.",
        "en": "TikTok blocked access to this link. Several methods were tried without success.",
        "ru": "TikTok заблокировал доступ к этой ссылке. Несколько способов не помогли.",
        "de": "TikTok hat den Zugriff auf diesen Link blockiert. Mehrere Methoden schlugen fehl.",
        "es": "TikTok bloqueó el acceso a este enlace. Se intentaron varios métodos sin éxito.",
        "fr": "TikTok a bloqué l'accès à ce lien. Plusieurs méthodes ont échoué.",
        "ar": "حظر TikTok الوصول إلى هذا الرابط. جُرّبت عدة طرق دون نجاح.",
    },
    "err_login": {
        "tr": "Bu içerik giriş gerektiriyor. Geçerli bir cookie dosyası gerekebilir.",
        "en": "This content requires login. A valid cookie file may be needed.",
        "ru": "Этот контент требует входа. Может понадобиться действительный файл cookie.",
        "de": "Dieser Inhalt erfordert eine Anmeldung. Eine gültige Cookie-Datei kann nötig sein.",
        "es": "Este contenido requiere inicio de sesión. Puede que necesites un archivo de cookies válido.",
        "fr": "Ce contenu nécessite une connexion. Un fichier cookie valide peut être requis.",
        "ar": "يتطلب هذا المحتوى تسجيل الدخول. قد تحتاج إلى ملف كوكيز صالح.",
    },
    "err_private": {
        "tr": "Bu içerik gizli veya giriş gerektiriyor.", "en": "This content is private or requires login.",
        "ru": "Этот контент приватный или требует входа.", "de": "Dieser Inhalt ist privat oder erfordert Anmeldung.",
        "es": "Este contenido es privado o requiere inicio de sesión.", "fr": "Ce contenu est privé ou nécessite une connexion.",
        "ar": "هذا المحتوى خاص أو يتطلب تسجيل الدخول.",
    },
    "err_format": {
        "tr": "Bu içerik için uygun indirme formatı bulunamadı.",
        "en": "No suitable download format was found for this content.",
        "ru": "Не найден подходящий формат загрузки для этого контента.",
        "de": "Kein passendes Download-Format für diesen Inhalt gefunden.",
        "es": "No se encontró un formato de descarga adecuado para este contenido.",
        "fr": "Aucun format de téléchargement adapté n'a été trouvé.",
        "ar": "لم يتم العثور على صيغة تنزيل مناسبة لهذا المحتوى.",
    },
    "err_ig_checkpoint": {
        "tr": "Instagram, botun kullandığı hesabı geçici olarak kilitledi. "
              "Bu içerik giriş gerektirdiği için şu an indirilemiyor. "
              "Yönetici doğrulamayı tamamladıktan sonra tekrar deneyin.",
        "en": "Instagram has temporarily locked the account the bot uses. "
              "This content requires login, so it can't be downloaded right now. "
              "Try again after the admin completes verification.",
        "ru": "Instagram временно заблокировал аккаунт бота. Этот контент требует "
              "входа, поэтому сейчас недоступен. Повторите после проверки администратором.",
        "de": "Instagram hat das Konto des Bots vorübergehend gesperrt. Dieser Inhalt "
              "erfordert eine Anmeldung und ist derzeit nicht ladbar. Bitte später erneut versuchen.",
        "es": "Instagram bloqueó temporalmente la cuenta del bot. Este contenido requiere "
              "inicio de sesión, así que no se puede descargar ahora. Inténtalo más tarde.",
        "fr": "Instagram a temporairement verrouillé le compte du bot. Ce contenu nécessite "
              "une connexion et ne peut pas être téléchargé pour l'instant. Réessayez plus tard.",
        "ar": "قام إنستغرام بقفل حساب البوت مؤقتًا. يتطلب هذا المحتوى تسجيل الدخول، "
              "لذا لا يمكن تنزيله الآن. حاول مرة أخرى لاحقًا.",
    },
    "profile_link_unsupported": {
        "tr": "Bu bir profil bağlantısı, tekil bir gönderi değil. Lütfen indirmek istediğin gönderinin/videonun kendi bağlantısını gönder.",
        "en": "This is a profile link, not a single post. Please send the link to the specific post/video you want to download.",
        "ru": "Это ссылка на профиль, а не на отдельный пост. Отправьте ссылку на конкретный пост/видео.",
        "de": "Das ist ein Profil-Link, kein einzelner Beitrag. Bitte sende den Link zum gewünschten Beitrag/Video.",
        "es": "Este es un enlace de perfil, no de una publicación. Envía el enlace a la publicación/video que quieres descargar.",
        "fr": "Ceci est un lien de profil, pas d'une publication. Envoyez le lien de la publication/vidéo à télécharger.",
        "ar": "هذا رابط ملف شخصي وليس منشورًا واحدًا. أرسل رابط المنشور/الفيديو الذي تريد تنزيله.",
    },
    "err_unsupported": {
        "tr": "Bu bağlantı desteklenmiyor.", "en": "This link is not supported.",
        "ru": "Эта ссылка не поддерживается.", "de": "Dieser Link wird nicht unterstützt.",
        "es": "Este enlace no es compatible.", "fr": "Ce lien n'est pas pris en charge.",
        "ar": "هذا الرابط غير مدعوم.",
    },
    "err_restricted": {
        "tr": "Bu gönderi yalnızca belirli kişilere açık (yaş / kitle sınırı). Bot erişemiyor.",
        "en": "This post is limited to certain viewers (age or audience restriction).",
        "ru": "Эта публикация доступна только определённым зрителям (возрастное ограничение).",
        "de": "Dieser Beitrag ist nur für bestimmte Zuschauer sichtbar (Alters-/Zielgruppenlimit).",
        "es": "Esta publicación está limitada a ciertos espectadores (restricción de edad o audiencia).",
        "fr": "Ce post est réservé à certains spectateurs (restriction d'âge ou d'audience).",
        "ar": "هذا المنشور مقصور على جمهور معيّن (قيد عمري أو جمهور محدد).",
    },
    "err_no_media": {
        "tr": "Bu gönderide indirilecek bir medya yok — sadece metin.",
        "en": "There's no media in this post — it's text only.",
        "ru": "В этой публикации нет медиа — только текст.",
        "de": "Dieser Beitrag enthält keine Medien — nur Text.",
        "es": "Esta publicación no tiene medios — solo texto.",
        "fr": "Ce post ne contient aucun média — uniquement du texte.",
        "ar": "لا توجد وسائط في هذا المنشور — نص فقط.",
    },
    "err_generic": {
        "tr": "Bu bağlantı indirilemedi. Link silinmiş, gizli veya erişim kısıtlı olabilir.",
        "en": "This link couldn't be downloaded. It may be deleted, private or restricted.",
        "ru": "Не удалось скачать ссылку. Возможно, она удалена, приватна или ограничена.",
        "de": "Dieser Link konnte nicht heruntergeladen werden. Evtl. gelöscht, privat oder gesperrt.",
        "es": "No se pudo descargar este enlace. Puede estar eliminado, ser privado o estar restringido.",
        "fr": "Impossible de télécharger ce lien. Il est peut-être supprimé, privé ou restreint.",
        "ar": "تعذّر تنزيل هذا الرابط. قد يكون محذوفًا أو خاصًا أو مقيّدًا.",
    },
    "err_unexpected": {
        "tr": "Beklenmeyen bir hata oluştu. Link bozuk, gizli veya geçici erişim sorunu olabilir.",
        "en": "An unexpected error occurred. The link may be broken, private or temporarily unavailable.",
        "ru": "Произошла непредвиденная ошибка. Ссылка может быть повреждена, приватна или временно недоступна.",
        "de": "Ein unerwarteter Fehler ist aufgetreten. Der Link ist evtl. defekt, privat oder vorübergehend nicht verfügbar.",
        "es": "Ocurrió un error inesperado. El enlace puede estar roto, ser privado o no estar disponible temporalmente.",
        "fr": "Une erreur inattendue s'est produite. Le lien est peut-être cassé, privé ou temporairement indisponible.",
        "ar": "حدث خطأ غير متوقع. قد يكون الرابط معطوبًا أو خاصًا أو غير متاح مؤقتًا.",
    },
}
