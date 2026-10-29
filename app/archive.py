"""Private forum archive with durable provisioning and one topic per known sender."""
import asyncio
import re
import time
from telethon import functions, types, errors
from message_metadata import label, render, rich_text

AUTH_ERRORS = (errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
               errors.AuthKeyDuplicatedError, errors.SessionExpiredError, errors.UserDeactivatedError)


class ForumArchive:
    def __init__(self, store, client, network, logger):
        self.store, self.client, self.network, self.logger = store, client, network, logger
        self.lock = asyncio.Lock()
        self.root_lock = asyncio.Lock()
        self.validated = False
        self.names, self.cleaned_topics = {}, set()
        self.contact_cached_keys = set()

    async def display_name(self, key, fallback):
        if key.startswith('user:'):
            contact = await asyncio.to_thread(self.store.contact_title,int(key.split(':')[1]))
            if contact:
                contact = label(contact,90)
                if self.names.get(key) != contact:
                    self.cleaned_topics.discard(key)
                self.names[key] = contact
                self.contact_cached_keys.add(key)
                return contact
            if key in self.contact_cached_keys:
                self.contact_cached_keys.discard(key)
                self.names.pop(key,None)
                self.cleaned_topics.discard(key)
        if key in self.names:
            return self.names[key]
        title = fallback
        if key.startswith('user:'):
            try:
                user_id = int(key.split(':')[1])
                user = await self.network.call(lambda: self.client.get_entity(user_id))
                first, last, username = (getattr(user,k,None) for k in ('first_name','last_name','username'))
                full = ' '.join(filter(None,(first,last)))
                title = label(full + (' @'+username if username else ''),90) or fallback
                await asyncio.to_thread(self.store.enrich_user,user_id,first,last,username)
            except (errors.FloodWaitError, *AUTH_ERRORS):
                raise
            except Exception as error:
                self.logger.warning('Archive author lookup unavailable type=%s',type(error).__name__)
        self.names[key] = title
        return title

    async def edit_title(self, request):
        try:
            return await self.network.call(lambda: self.client(request))
        except errors.BadRequestError as error:
            if getattr(error,'message','') not in ('CHAT_NOT_MODIFIED','TOPIC_NOT_MODIFIED','TOPIC_TITLE_NOT_MODIFIED'):
                raise

    def refresh_author(self, job):
        name = self.names.get(job.get('archive_key'))
        if not name:
            return job['text']
        head, separator, body = job['text'].partition('\n\n')
        head = re.sub(r'(?m)^(?:Автор:|👤) .*$',lambda _: '👤 '+name,head)
        return head + (separator+body if separator else '')

    @staticmethod
    def private_forum(entity):
        return (getattr(entity, 'creator', False) and getattr(entity, 'megagroup', False)
                and getattr(entity, 'forum', False) and not getattr(entity, 'username', None)
                and not any(getattr(u, 'active', False) for u in (getattr(entity, 'usernames', None) or [])))

    async def root(self):
        async with self.root_lock:
            return await self._root()

    async def _root(self):
        route = await asyncio.to_thread(self.store.archive_route, 'root', 'Архив Telegram')
        if route['target_id'] is None:
            title = route['title'] + ' · ' + route['marker']
            async def find():
                async for dialog in self.client.iter_dialogs():
                    if dialog.name == title and self.private_forum(dialog.entity):
                        return dialog.entity
                return None
            # Recover a group created before a crash/timeout instead of blindly creating again.
            group = await self.network.call(find, timeout=120)
            if group is None:
                result = await self.network.call(lambda: self.client(functions.channels.CreateChannelRequest(
                    title=title, about='Личный архив восстановленных сообщений. ' + route['marker'],
                    megagroup=True, forum=True)))
                groups = [c for c in result.chats if self.private_forum(c)]
                if len(groups) != 1:
                    raise RuntimeError('Private archive forum creation not confirmed')
                group = groups[0]
            await asyncio.to_thread(self.store.save_archive_route, 'root', group.id, group.access_hash)
            route = await asyncio.to_thread(self.store.archive_route, 'root', 'Архив Telegram')
            self.logger.info('Private archive forum ready')
        peer = types.InputPeerChannel(route['target_id'], route['access_hash'])
        if not self.validated:
            entity = await self.network.call(lambda: self.client.get_entity(peer))
            if not self.private_forum(entity):
                raise RuntimeError('Archive must remain a private forum owned by this account')
            if getattr(entity,'title',None) != 'Архив Telegram':
                await self.edit_title(functions.channels.EditTitleRequest(peer,'Архив Telegram'))
            self.validated = True
        return peer, route

    async def destination(self, job):
        async with self.lock:
            peer, root = await self.root()
            key = job.get('archive_key') or 'misc'
            display = await self.display_name(key,job.get('archive_title') or 'Без определённого автора')
            route = await asyncio.to_thread(self.store.archive_route,
                key, display)
            if route['topic_id'] is None:
                title = route['title'] + ' · ' + route['marker']
                topics = await self.network.call(lambda: self.client(functions.messages.GetForumTopicsRequest(
                    peer=peer, offset_date=None, offset_id=0, offset_topic=0, limit=100, q=route['marker'])))
                matches = [t for t in topics.topics if getattr(t, 'title', None) == title]
                if len(matches) > 1:
                    raise RuntimeError('Ambiguous archive topic')
                topic_id = matches[0].id if matches else None
                if topic_id is None:
                    updates = await self.network.call(lambda: self.client(functions.messages.CreateForumTopicRequest(
                        peer=peer, title=title, random_id=route['random_id'])))
                    ids = [u.id for u in updates.updates if isinstance(u, types.UpdateMessageID)
                           and u.random_id == route['random_id']]
                    if not ids:
                        ids = [u.message.id for u in updates.updates
                               if isinstance(u, types.UpdateNewChannelMessage)
                               and isinstance(getattr(u.message, 'action', None), types.MessageActionTopicCreate)]
                    if len(set(ids)) != 1:
                        raise RuntimeError('Archive topic creation not confirmed')
                    topic_id = ids[0]
                await asyncio.to_thread(self.store.save_archive_route, route['route_key'],
                    root['target_id'], root['access_hash'], topic_id)
                self.logger.info('Archive topic ready')
            else:
                topic_id = route['topic_id']
            if key not in self.cleaned_topics:
                await self.edit_title(functions.messages.EditForumTopicRequest(
                    peer=peer,topic_id=topic_id,title=display))
                await asyncio.to_thread(self.store.set_archive_title,key,display)
                self.cleaned_topics.add(key)
            return peer, dict(reply_to=topic_id)

    async def maintain(self):
        while True:
            try:
                peer, _ = await self.root()
                routes = await asyncio.to_thread(self.store.archive_topics)
                for route in routes:
                    await self.destination(dict(archive_key=route['route_key'],archive_title=route['title']))
                # Upgrade only our own generated legacy entries; never alter user text by guessing IDs.
                async def recent():
                    found = {}
                    entries = [(None,None)]+[(r['topic_id'],r['route_key']) for r in routes[:20]]
                    for topic,key in entries:
                        options = dict(reply_to=topic) if topic else {}
                        async for message in self.client.iter_messages(peer,limit=200,**options):
                            found[(False,message.id)] = (peer,key,message)
                    async for message in self.client.iter_messages('me',limit=200):
                        found[(True,message.id)] = ('me',None,message)
                    return list(found.values())
                messages = await self.network.call(recent,timeout=120)
                me = await self.network.identity(self.client)
                updated = 0
                for target,source_key,message in messages:
                    text = getattr(message,'raw_text','') or ''
                    own = getattr(message,'out',False) or getattr(message,'sender_id',None)==me.id
                    if not own or not text.startswith(('📂 Архив ранее','❌ Сообщение удалено','📎 Сохранённое медиа','✏️ Сообщение изменено')):
                        continue
                    header = text.partition('\n\n')[0]
                    found = re.search(r'(?m)^ID чата: (-?\d+); сообщение: (\d+)$',header)
                    if not found:
                        body = self.refresh_author(dict(archive_key=source_key,text=text)) if source_key else text
                    else:
                        row = await asyncio.to_thread(self.store.archive_record,int(found[1]),int(found[2]))
                        if not row:
                            continue
                        _, separator, original_body = text.partition('\n\n')
                        if not separator:
                            continue
                        row['text'] = original_body  # Keep this posted snapshot/chunk, not later DB content.
                        if text.startswith('✏️ Сообщение изменено') and not original_body.startswith('Было:\n'):
                            row['text'] = 'Было:\n' + original_body
                        body = text.split('\n',1)[0]+'\n'+render(row)
                    if getattr(message,'media',None):
                        body = body[:450]
                    plain, entities = rich_text(body)
                    if plain == text and entities == (getattr(message,'entities',None) or []):
                        continue
                    try:
                        await self.network.call(lambda: self.client.edit_message(target,message.id,plain,
                            formatting_entities=entities,parse_mode=None,link_preview=False))
                    except errors.MessageNotModifiedError:
                        pass
                    updated += 1
                    await asyncio.sleep(1)
                await asyncio.to_thread(self.store.heartbeat,'archive_layout_ok',time.time())
                self.logger.info('Archive layout checked inspected=%s updated_messages=%s topics=%s',len(messages),updated,len(routes))
                await asyncio.sleep(3600)
            except AUTH_ERRORS:
                raise
            except Exception as error:
                code = getattr(error,'message','')
                code = code if re.fullmatch(r'[A-Z_]{1,80}',code) else 'UNKNOWN'
                self.logger.warning('Archive layout maintenance pending type=%s rpc=%s',type(error).__name__,code)
                await asyncio.sleep(60)
