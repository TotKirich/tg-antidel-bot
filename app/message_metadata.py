"""Capture origin and content type without network lookups or invented authors."""
from datetime import datetime, timezone, timedelta
from html import escape

from telethon import types, utils

EXTRA_FIELDS = ('chat_title', 'chat_username', 'chat_type', 'sender_kind', 'message_kind',
                'content_summary', 'grouped_id', 'reply_to_id', 'source_date', 'forwarded_origin')


def label(value, limit=256):
    return ' '.join(str(value or '').split())[:limit]


def name(entity):
    return label(getattr(entity, 'title', None) or ' '.join(filter(None,
        (getattr(entity, 'first_name', None), getattr(entity, 'last_name', None)))))


def content_type(message):
    media = getattr(message, 'media', None)
    if isinstance(media, types.MessageMediaPhoto):
        return 'photo', 'Фото без подписи'
    if isinstance(media, types.MessageMediaDocument):
        document = getattr(media, 'document', None)
        attributes = getattr(document, 'attributes', [])
        filename, kind, title = '', 'document', 'Документ'
        for attribute in attributes:
            if isinstance(attribute, types.DocumentAttributeFilename):
                filename = label(attribute.file_name)
            elif isinstance(attribute, types.DocumentAttributeAudio):
                kind, title = ('voice', 'Голосовое сообщение') if attribute.voice else ('audio', 'Аудио')
            elif isinstance(attribute, types.DocumentAttributeVideo):
                kind, title = ('video_note', 'Видеосообщение (кружок)') if attribute.round_message else ('video', 'Видео')
            elif isinstance(attribute, types.DocumentAttributeSticker):
                kind, title = 'sticker', 'Стикер'
            elif isinstance(attribute, types.DocumentAttributeAnimated) and kind == 'document':
                kind, title = 'animation', 'Анимация'
        return kind, title + (f': {filename}' if filename else '')
    if isinstance(media, types.MessageMediaPoll):
        question = media.poll.question
        return 'poll', 'Опрос: ' + str(getattr(question, 'text', question))
    if isinstance(media, types.MessageMediaContact):
        return 'contact', 'Контакт: ' + label(f'{media.first_name} {media.last_name or ""} {media.phone_number}')
    if isinstance(media, (types.MessageMediaGeo, types.MessageMediaGeoLive, types.MessageMediaVenue)):
        geo = getattr(media, 'geo', None)
        return 'location', f"Местоположение: {getattr(geo, 'lat', '?')}, {getattr(geo, 'long', '?')}"
    if isinstance(media, types.MessageMediaDice):
        return 'dice', f'Игровое сообщение: {media.emoticon} {media.value}'
    if isinstance(media, types.MessageMediaWebPage):
        return 'webpage', 'Предпросмотр ссылки'
    if media is not None:
        return 'unsupported', f'Содержимое типа {type(media).__name__}; исходные метаданные сохранены'
    if getattr(message, 'action', None) is not None:
        return 'service', f'Служебное событие: {type(message.action).__name__}'
    if getattr(message, 'raw_text', None):
        return 'text', ''
    return 'empty', 'Пустое сообщение или содержимое не распознано'


def capture(message, sender=None, chat=None):
    chat = chat or getattr(message, 'chat', None)
    sender = sender or getattr(message, 'sender', None)
    chat_id = message.chat_id
    sender_id = getattr(message, 'sender_id', None)
    if sender_id is None and getattr(message, 'post', False):
        sender_id = chat_id  # A channel post, not an unknown individual.
    if chat_id > 0:
        chat_type = 'private'
    elif isinstance(chat, types.Channel):
        chat_type = 'supergroup' if chat.megagroup else 'channel'
    elif getattr(message, 'post', False):
        chat_type = 'channel'
    else:
        chat_type = 'group' if chat_id > -1000000000000 else 'channel_or_supergroup'
    kind, summary = content_type(message)
    forwarded = getattr(message, 'fwd_from', None)
    forwarded_origin = label(getattr(forwarded, 'from_name', None))
    if forwarded and getattr(forwarded, 'from_id', None):
        forwarded_origin = f'ID {utils.get_peer_id(forwarded.from_id)}'
    if forwarded and not forwarded_origin:
        forwarded_origin = 'источник скрыт'
    date = getattr(message, 'date', None)
    raw = bytes(message) if isinstance(message, types.Message) else None
    if raw and len(raw) > 1024 * 1024:
        raw = None
    return dict(chat_id=chat_id, message_id=message.id, user_id=sender_id,
        username=getattr(sender, 'username', None), first_name=getattr(sender, 'first_name', None) or getattr(sender, 'title', None),
        last_name=getattr(sender, 'last_name', None), text=getattr(message, 'raw_text', None) or '',
        file_path=None, date_sent=datetime.now().isoformat(), chat_title=name(chat),
        chat_username=getattr(chat, 'username', None), chat_type=chat_type,
        sender_kind='user' if sender_id and sender_id > 0 else 'chat' if sender_id else 'unknown',
        message_kind=kind, content_summary=summary, grouped_id=getattr(message, 'grouped_id', None),
        reply_to_id=getattr(message, 'reply_to_msg_id', None), source_date=date.isoformat() if date else None,
        forwarded_origin=forwarded_origin, raw_message=raw)


def field(row, key, default=None):
    return row[key] if key in row.keys() and row[key] is not None else default


def author(row):
    display = label(' '.join(str(field(row, k, '')) for k in ('first_name', 'last_name')))
    username = field(row, 'username')
    if username:
        display += ' @' + label(username)
    user_id = field(row, 'user_id')
    return display.strip() or (f'Пользователь {user_id}' if user_id is not None else 'Автор неизвестен')


def short_date(value):
    try:
        date = datetime.fromisoformat(str(value))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m %H:%M')
    except (ValueError, TypeError):
        return ''


def rich_text(text):
    from telethon.extensions import html
    head, separator, body = text.partition('\n\n')
    title, newline, metadata = head.partition('\n')
    markup = '<b>' + escape(title) + '</b>' + ('\n' + escape(metadata) if newline else '')
    if separator:
        sections = edit_sections(text, body)
        for heading, content in sections:
            attribute = ' expandable' if len(content) > 700 else ''
            markup += '\n\n' + ('<b>' + escape(heading) + '</b>\n' if heading else '')
            markup += '<blockquote' + attribute + '>' + escape(content) + '</blockquote>'
    return html.parse(markup)


def edit_sections(text, body):
    if text.startswith('✏️ Сообщение изменено'):
        if body.startswith('Было:\n'):
            before, separator, after = body[6:].partition('\n\nСтало:\n')
            return [('Было:', before)] + ([('Стало:', after)] if separator else [])
        if body.startswith('Стало:\n'):
            return [('Стало:', body[7:])]
    return [('', body)]


def archive_chunks(text, limit=3900):
    """Keep context and section labels in every part; count Telegram UTF-16 units."""
    units = lambda value: len(value.encode('utf-16-le')) // 2
    if units(text) <= limit:
        return [text]
    head, separator, body = text.partition('\n\n')
    if not separator:
        head, body = text.split('\n', 1) if '\n' in text else ('Архив', text)
    # Reserve space for part numbering and the longest section label.
    budget = limit - units(head) - 70
    if budget < 100:
        raise ValueError('Archive metadata exceeds message budget')
    parts = []
    for heading, content in edit_sections(text, body):
        remaining = content
        while remaining:
            used = end = 0
            for char in remaining:
                width = 2 if ord(char) > 0xffff else 1
                if used + width > budget:
                    break
                used += width
                end += 1
            if end < len(remaining):
                # Prefer paragraph, line, then word boundaries near the end.
                for boundary in ('\n\n', '\n', ' '):
                    split = remaining.rfind(boundary, end // 2, end)
                    if split >= 0:
                        end = split + len(boundary)
                        break
            parts.append((heading, remaining[:end]))
            remaining = remaining[end:]
        if not content:
            parts.append((heading, '[Без текстовой подписи]'))
    return [head + f'\n📄 Часть {i}/{len(parts)}\n\n' +
            (heading + '\n' if heading else '') + content
            for i, (heading, content) in enumerate(parts, 1)]


def render(row, media_pending=False):
    title = field(row, 'chat_title') or 'Чат без названия'
    username = field(row, 'chat_username')
    origin = f'👤 {author(row)}'
    if field(row, 'chat_type') != 'private':
        origin += f'\n💬 {label(title)}' + (f' @{label(username)}' if username else '')
    date = short_date(field(row, 'source_date') or field(row, 'date_sent'))
    if date:
        origin += '\n🕒 ' + date
    if field(row, 'grouped_id'):
        origin += '\n📷 Часть медиаальбома'
    if field(row, 'forwarded_origin'):
        origin += '\nПереслано от: ' + label(row['forwarded_origin'])
    text = field(row, 'text', '')
    if not text.strip():
        text = field(row, 'content_summary') or ('Медиафайл; текст/подпись отсутствуют в старой записи'
            if field(row, 'file_path') else 'Содержимое отсутствует в старой записи; исходный тип неизвестен')
    if media_pending:
        text += '\n[Медиа ожидает скачивания или повторной попытки]'
    return origin + '\n\n' + text
