import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from telethon import functions, types, errors
from archive import ForumArchive
from storage import Store, disk_status
from transport import Delivery, Network
from message_metadata import rich_text, short_date, render, archive_chunks


class FakeClient:
    def __init__(self):
        self.group = NS(id=100, access_hash=88, creator=True, megagroup=True, forum=True, username=None)
        self.groups, self.topics, self.requests = [], [], []
        self.send_message, self.send_file = AsyncMock(), AsyncMock()
        self.get_me = AsyncMock(return_value=NS(id=42))

    async def iter_dialogs(self):
        for group in self.groups:
            yield NS(name=group.title, entity=group)

    async def get_entity(self, peer):
        return self.group

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.channels.CreateChannelRequest):
            self.group.title = request.title
            self.groups.append(self.group)
            return NS(chats=[self.group])
        if isinstance(request, functions.messages.GetForumTopicsRequest):
            return NS(topics=self.topics)
        if isinstance(request, functions.messages.CreateForumTopicRequest):
            topic = NS(id=500 + len(self.topics), title=request.title)
            self.topics.append(topic)
            return NS(updates=[types.UpdateMessageID(id=topic.id, random_id=request.random_id)])
        if isinstance(request,functions.channels.EditTitleRequest):
            self.group.title=request.title
            return NS()
        if isinstance(request,functions.messages.EditForumTopicRequest):
            for topic in self.topics:
                if topic.id==request.topic_id:topic.title=request.title
            return NS()
        raise AssertionError(type(request).__name__)


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'db')
        self.client = FakeClient()
        self.network = Network()

    def save(self, chat, mid, user=42, **kwargs):
        self.store.save(dict(chat_id=chat, message_id=mid, user_id=user, username='fixture',
            first_name='Fixture', last_name='', text='saved', file_path=None,
            date_sent=datetime.now().isoformat(), **kwargs))

    async def test_one_topic_per_user_across_chats_and_restart(self):
        self.save(10, 1)
        self.save(-123, 2)
        self.store.deleted(10, 1)
        self.store.deleted(-123, 2)
        jobs=[j for j in self.store.pending() if j['kind']=='archive']
        self.assertEqual([j['archive_key'] for j in jobs], ['user:42','user:42'])
        archive=ForumArchive(self.store,self.client,self.network,Mock())
        first=await archive.destination(jobs[0])
        reopened=ForumArchive(Store(self.store.path),self.client,self.network,Mock())
        second=await reopened.destination(jobs[1])
        self.assertEqual(first[1],second[1])
        self.assertEqual(len(self.client.groups),1)
        self.assertEqual(len(self.client.topics),1)
        request=next(r for r in self.client.requests if isinstance(r,functions.channels.CreateChannelRequest))
        self.assertTrue(request.megagroup and request.forum)
        self.assertTrue(self.store.is_archive_chat(-1000000000100))
        self.assertFalse(self.store.is_archive_chat(42))

    async def test_different_users_have_different_topics(self):
        archive=ForumArchive(self.store,self.client,self.network,Mock())
        first=await archive.destination(dict(archive_key='user:1',archive_title='One'))
        second=await archive.destination(dict(archive_key='user:2',archive_title='Two'))
        self.assertNotEqual(first[1],second[1])
        self.assertEqual(len(self.client.groups),1)

    async def test_provisioning_recovers_topic_after_lost_local_commit(self):
        archive=ForumArchive(self.store,self.client,self.network,Mock())
        await archive.root()
        save=self.store.save_archive_route
        def fail(key,*args):
            if key=='user:42':raise OSError('fixture crash')
            return save(key,*args)
        job=dict(archive_key='user:42',archive_title='Fixture')
        with patch.object(self.store,'save_archive_route',side_effect=fail):
            with self.assertRaises(OSError):await archive.destination(job)
        destination=await ForumArchive(Store(self.store.path),self.client,self.network,Mock()).destination(job)
        self.assertEqual(destination[1]['reply_to'],500)
        self.assertEqual(len(self.client.topics),1)

    async def test_saved_group_can_be_recovered_before_local_commit(self):
        route=self.store.archive_route('root','Архив Telegram')
        self.client.group.title=route['title']+' · '+route['marker']
        self.client.groups=[self.client.group]
        await ForumArchive(self.store,self.client,self.network,Mock()).root()
        self.assertFalse(any(isinstance(r,functions.channels.CreateChannelRequest) for r in self.client.requests))

    async def test_public_or_unowned_archive_is_rejected(self):
        self.store.archive_route('root','Archive')
        self.store.save_archive_route('root',100,88)
        self.client.group.username='public'
        with self.assertRaises(RuntimeError):
            await ForumArchive(self.store,self.client,self.network,Mock()).root()

    async def test_replay_is_durable_and_does_not_notify_twice(self):
        self.save(10,1)
        self.store.deleted(10,1)
        with self.store.connection() as db:db.execute('DELETE FROM outbox')
        self.assertEqual(self.store.replay_deleted_archive(),1)
        self.assertEqual(Store(self.store.path).replay_deleted_archive(),0)
        jobs=self.store.pending()
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['archive_key'],'user:42')
        self.assertEqual(jobs[0]['kind'],'archive')

    async def test_media_and_text_are_sent_into_same_topic(self):
        path=self.root/'media.bin';path.write_bytes(b'fixture')
        self.save(10,1)
        self.store.attach_media(10,1,path)
        self.store.deleted(10,1)
        delivery=Delivery(self.store,self.client,self.network,
            dict(log_chat_id='me',archive_mode='forum',delivery_interval_seconds=0),lambda:{},Mock())
        await delivery.once()
        text=self.client.send_message.call_args
        media=self.client.send_file.call_args
        self.assertEqual(text.kwargs['reply_to'],media.kwargs['reply_to'])
        self.assertEqual(text.args[0].channel_id,100)

    async def test_status_reports_database_and_wal_disk_usage(self):
        self.save(10,1)
        status=Store.read_status(self.store.path)
        self.assertEqual(status['db_bytes'],Path(self.store.path).stat().st_size)
        self.assertGreaterEqual(status['db_total_bytes'],status['db_bytes'])

    async def test_channel_without_person_has_its_own_source_topic(self):
        self.save(-1000000000042,1,user=-1000000000042,chat_type='channel',sender_kind='chat')
        self.store.deleted(-1000000000042,1)
        job=next(j for j in self.store.pending() if j['kind']=='archive')
        self.assertEqual(job['archive_key'],'chat:-1000000000042')

    async def test_early_story_and_messages_share_user_topic(self):
        import time
        self.store.story(42,1,'fixture',time.time(),time.time()+3600)
        self.store.story_removed(42,1,explicit=True)
        self.assertEqual(next(j for j in self.store.pending() if j['kind']=='archive')['archive_key'],'user:42')

    async def test_schema_five_migration_preserves_queued_job(self):
        with self.store.connection() as db:
            self.store.enqueue(db,'legacy','archive','fixture')
            db.execute('ALTER TABLE outbox DROP COLUMN archive_key')
            db.execute('ALTER TABLE outbox DROP COLUMN archive_title')
            db.execute('DROP TABLE archive_routes')
            db.execute('DROP TABLE archive_replayed')
            db.execute('PRAGMA user_version=5')
        reopened=Store(self.store.path)
        self.assertEqual(reopened.pending()[0]['event_key'],'legacy')
        self.assertIsNone(reopened.pending()[0]['archive_key'])

    async def test_disk_status_includes_media_and_capacity(self):
        media=self.root/'media';media.mkdir()
        (media/'fixture.bin').write_bytes(b'x'*12345)
        (self.root/'other.bin').write_bytes(b'y'*6789)
        usage=NS(total=100000,used=90000,free=10000)
        with patch('storage.shutil.disk_usage',return_value=usage):
            result=disk_status(self.root)
        self.assertEqual(result['media_bytes'],12345)
        self.assertGreaterEqual(result['data_bytes'],12345+6789)
        self.assertEqual(result['disk_free_bytes'],10000)
        self.assertEqual(result['disk_total_bytes'],100000)

    async def test_known_person_names_replace_ids_and_markers_in_topic(self):
        original=self.client.get_entity
        async def entity(peer):
            if isinstance(peer,int):return NS(first_name='Alice',last_name='Smith',username='alice')
            return await original(peer)
        self.client.get_entity=entity
        archive=ForumArchive(self.store,self.client,self.network,Mock())
        await archive.destination(dict(archive_key='user:42',archive_title='Пользователь 42'))
        self.assertEqual(self.client.group.title,'Архив Telegram')
        self.assertEqual(self.client.topics[0].title,'Alice Smith @alice')
        self.assertEqual(archive.refresh_author(dict(archive_key='user:42',text='Удалено\n👤 Пользователь 42\n\ntext')),
            'Удалено\n👤 Alice Smith @alice\n\ntext')

    async def test_rich_format_preserves_untrusted_text_and_emojis(self):
        original='❌ Удалено\n👤 Alice\n\n<script>&😀</script>'
        text,entities=rich_text(original)
        self.assertEqual(text,original)
        self.assertTrue(any(isinstance(e,types.MessageEntityBold) for e in entities))
        self.assertTrue(any(isinstance(e,types.MessageEntityBlockquote) for e in entities))
        self.assertFalse(any(isinstance(e,types.MessageEntityTextUrl) for e in entities))
        _,long_entities=rich_text('Удалено\n\n'+'😀'*701)
        self.assertTrue(next(e for e in long_entities if isinstance(e,types.MessageEntityBlockquote)).collapsed)
        self.assertEqual(short_date('2026-10-27T12:44:13+00:00'),'27.10 15:44')

    async def test_edit_has_two_independent_quotes(self):
        original='✏️ Сообщение изменено\n👤 Alice\n\nБыло:\nold <b>😀\n\nСтало:\nnew & text'
        text,entities=rich_text(original)
        self.assertEqual(text,original)
        quoted=[]
        encoded=text.encode('utf-16-le')
        for entity in entities:
            if isinstance(entity,types.MessageEntityBlockquote):
                quoted.append(encoded[entity.offset*2:(entity.offset+entity.length)*2].decode('utf-16-le'))
        self.assertEqual(quoted,['old <b>😀','new & text'])

    async def test_archive_parts_keep_all_text_and_context(self):
        content=('A paragraph with 😀 emoji and words.\n\n'*250)
        header='❌ Сообщение удалено\n👤 Alice\n🕒 28.10 10:00'
        parts=archive_chunks(header+'\n\n'+content)
        self.assertGreater(len(parts),1)
        self.assertEqual(''.join(p.partition('\n\n')[2] for p in parts),content)
        for part in parts:
            self.assertTrue(part.startswith(header+'\n📄 Часть '))
            self.assertLessEqual(len(part.encode('utf-16-le'))//2,3900)
            self.assertEqual(len([e for e in rich_text(part)[1] if isinstance(e,types.MessageEntityBlockquote)]),1)
        edit='✏️ Сообщение изменено\n👤 Alice\n\nБыло:\n'+content+'\n\nСтало:\n'+content
        for part in archive_chunks(edit):
            body=part.partition('\n\n')[2]
            self.assertTrue(body.startswith(('Было:\n','Стало:\n')))
        self.assertEqual(len(archive_chunks(header+'\n\n'+'a'*2000)),1)

    async def test_existing_generated_archive_text_is_updated_in_place(self):
        self.save(10,1)
        legacy=NS(id=600,out=False,sender_id=42,media=None,raw_text='❌ Сообщение удалено\nID чата: 10; сообщение: 1\n\nold')
        async def messages(*args,**kwargs):
            yield legacy
        self.client.iter_messages=messages
        self.client.edit_message=AsyncMock()
        archive=ForumArchive(self.store,self.client,self.network,Mock())
        task=asyncio.create_task(archive.maintain())
        try:
            for _ in range(100):
                if self.client.edit_message.await_count:break
                await asyncio.sleep(.01)
            self.assertEqual(self.client.edit_message.call_args.args[1],600)
            text=self.client.edit_message.call_args.args[2]
            self.assertNotIn('ID чата:',text)
            self.assertIn('old',text)
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_already_current_topic_title_is_idempotent(self):
        archive=ForumArchive(self.store,self.client,self.network,Mock())
        async def unchanged(request):
            raise errors.BadRequestError(request=None,message='TOPIC_NOT_MODIFIED')
        original=archive.client
        archive.client=unchanged
        await archive.edit_title(functions.messages.EditForumTopicRequest(
            peer=types.InputPeerChannel(100,88),topic_id=500,title='Fixture'))
        archive.client=original

    async def test_legacy_edit_preserves_both_original_snapshots(self):
        self.save(10,1)
        legacy=NS(id=601,out=True,media=None,raw_text='✏️ Сообщение изменено\nID чата: 10; сообщение: 1\n\nold <text>\n\nСтало:\nnew & text')
        async def messages(*args,**kwargs):yield legacy
        self.client.iter_messages=messages
        self.client.edit_message=AsyncMock()
        archive=ForumArchive(self.store,self.client,self.network,Mock())
        task=asyncio.create_task(archive.maintain())
        try:
            for _ in range(100):
                if self.client.edit_message.await_count:break
                await asyncio.sleep(.01)
            text=self.client.edit_message.call_args.args[2]
            self.assertIn('Было:\nold <text>',text)
            self.assertIn('Стало:\nnew & text',text)
            self.assertNotIn('ID чата:',text)
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_contact_alias_is_preferred_without_replacing_profile_history(self):
        self.save(10,1)
        self.store.replace_contacts([(42,'Мой контакт','Личное имя','contactlogin')])
        self.store.deleted(10,1)
        job=next(j for j in self.store.pending() if j['kind']=='archive')
        self.assertIn('Мой контакт Личное имя @contactlogin',job['text'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT first_name FROM messages').fetchone()[0],'Fixture')
        archive=ForumArchive(self.store,self.client,self.network,Mock())
        self.assertEqual(await archive.display_name('user:42','Fallback'),'Мой контакт Личное имя @contactlogin')
        self.store.replace_contacts([(42,'Новое имя',None,None)])
        self.assertEqual(await archive.display_name('user:42','Fallback'),'Новое имя')
        self.store.replace_contacts([])
        self.assertEqual(await archive.display_name('user:42','Fallback'),'Fallback')

    async def test_saved_messages_legacy_copy_is_updated_in_place(self):
        self.save(10,1)
        self.store.replace_contacts([(42,'Из записной',None,'alias')])
        legacy=NS(id=700,out=True,media=None,raw_text='✏️ Сообщение изменено\nID чата: 10; сообщение: 1\n\nBefore\n\nСтало:\nAfter')
        async def messages(peer,**kwargs):
            if peer=='me':yield legacy
        self.client.iter_messages=messages
        self.client.edit_message=AsyncMock()
        archive=ForumArchive(self.store,self.client,self.network,Mock())
        task=asyncio.create_task(archive.maintain())
        try:
            for _ in range(100):
                if self.client.edit_message.await_count:break
                await asyncio.sleep(.01)
            call=self.client.edit_message.call_args
            self.assertEqual(call.args[:2],('me',700))
            self.assertIn('Из записной @alias',call.args[2])
            self.assertIn('Before',call.args[2])
            self.assertIn('After',call.args[2])
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
