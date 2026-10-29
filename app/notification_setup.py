"""Validate a notification bot before replacing its token; never log tokens."""
import re
from aiohttp import ClientSession, ClientTimeout
from runtime import owner_settings, read_config, update_config


class BotConfigurationError(ValueError):
    pass


async def check_bot(token):
    if not re.fullmatch(r'[0-9]{5,}:[A-Za-z0-9_-]{20,}', token):
        raise BotConfigurationError('Неверный формат токена BotFather')
    async with ClientSession(timeout=ClientTimeout(total=15)) as session:
        async with session.get('https://api.telegram.org/bot' + token + '/getMe') as response:
            data = await response.json()
            bot = data.get('result', {})
            if response.status != 200 or not data.get('ok') or not bot.get('is_bot') or not isinstance(bot.get('id'), int):
                raise BotConfigurationError('Telegram не подтвердил токен бота')
            return bot


async def configure(client, token):
    token = token.strip() or read_config().get('bot_token', '')
    bot = await check_bot(token)
    owner = await client.get_me()
    if owner is None or getattr(owner, 'bot', False):
        raise BotConfigurationError('Сначала войдите в пользовательский аккаунт')
    previous_owner = read_config().get('admin_user_id')
    if previous_owner is not None and previous_owner != owner.id:
        raise BotConfigurationError('Этот каталог данных принадлежит другому аккаунту. Для другого пользователя нужна новая пустая папка data.')
    def change(current):
        return dict(bot_token=token, bot_id=bot['id'], bot_username=bot.get('username', ''),
                    **owner_settings(current, owner.id))
    update_config(change)
    return bot.get('username', str(bot['id']))
