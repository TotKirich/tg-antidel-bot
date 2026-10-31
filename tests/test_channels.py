import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors, types
from channels import ChannelPolicy
from storage import Store
from transport import Delivery, Network
import notify_bot
import userbot
from runtime import DEFAULTS


CHANNEL = -1000000000123


def peer(number=123, **kwargs):
    return types.Channel(id=number, title='Fixture channel', photo=types.ChatPhotoEmpty(),
                         date=datetime.now(timezone.utc), broadcast=True, username='fixture_channel', **kwargs)


class Client:
    def __init__(self, entities=()):
        self.entities = entities
        self.get_entity = AsyncMock(return_value=peer())
        self.request = AsyncMock(return_value=NS(participant=types.ChannelParticipantSelf(
            user_id=42, inviter_id=42, date=datetime.now(timezone.utc))))

    def iter_dialogs(self, **kwargs):
        async def iterator():
            for entity in self.entities:
                if isinstance(entity, Exception):
                    raise entity
                yield NS(entity=entity)
        return iterator()

    async def __call__(self, query):
        return await self.request(query)


class ChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'messages.db')
        self.client = Client([peer()])
        self.policy = ChannelPolicy(self.client,Network(),self.store,Mock())

    def save(self, chat=CHANNEL, mid=1, payload=None):
        return self.store.save(dict(chat_id=chat,message_id=mid,user_id=chat,username='',first_name='',
            last_name='',text='original',file_path=None,date_sent=datetime.now().isoformat(),chat_type='channel'),
            media_payload=payload)

    async def test_viewed_nonmember_channel_is_not_saved_or_signalled(self):
        self.client.request.side_effect = errors.UserNotParticipantError(request=None)
        self.assertFalse(await self.policy.admit(CHANNEL,peer()))
        self.assertFalse(self.save(payload=b'fixture'))
        self.store.note_deletions(CHANNEL,[1,2])
        self.store.note_history_gap(CHANNEL,20)
        with self.store.connection() as db:
            for table in ('messages','media_jobs','deletion_signals','history_gaps','outbox'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    async def test_joined_channel_saved_and_overrides_survive_refresh_and_restart(self):
        await self.policy.refresh()
        self.assertTrue(self.save())
        self.store.set_channel_option(CHANNEL,'notifications_enabled',False)
        await self.policy.refresh()
        self.assertFalse(Store(self.root/'messages.db').channel_state(CHANNEL)['notifications_enabled'])
        self.assertTrue(self.store.channel_allowed(CHANNEL))
        self.assertFalse(self.store.channel_allowed(CHANNEL,notifications=True))

    async def test_unsubscribe_stops_edits_rechecks_media_and_legacy_delivery(self):
        await self.policy.refresh()
        self.save(payload=b'fixture')
        self.store.edit(CHANNEL,1,'changed')
        self.client.entities=[]
        await self.policy.refresh()
        self.assertFalse(self.store.edit(CHANNEL,1,'must not save'))
        self.assertEqual(self.store.pending_media(),[])
        self.assertEqual(self.store.recheck_candidates(),[])
        self.assertFalse(self.store.delivery_allowed(dict(kind='archive',event_key=f'forum-replay:{CHANNEL}:1')))
        sender=NS(send_message=AsyncMock())
        delivery=Delivery(self.store,sender,Network(),dict(log_chat_id='me',delivery_interval_seconds=0),lambda:{},Mock())
        await delivery.once()
        sender.send_message.assert_not_awaited()
        self.assertEqual(self.store.pending(),[])

    async def test_capture_off_blocks_all_paths_but_keeps_saved_data(self):
        await self.policy.refresh()
        self.save(payload=b'fixture')
        self.store.set_channel_option(CHANNEL,'capture_enabled',False)
        self.assertFalse(await self.policy.admit(CHANNEL))
        self.assertFalse(self.save(mid=2))
        self.assertFalse(self.store.deleted(CHANNEL,1))
        self.assertFalse(self.store.edit(CHANNEL,1,'new'))
        self.assertEqual(self.store.pending_media(),[])
        self.assertEqual(self.store.recheck_candidates(),[])
        self.assertEqual(self.store.archive_record(CHANNEL,1)['text'],'original')
        self.client.request.assert_not_awaited()

    async def test_notification_off_keeps_archive_and_filters_pending_notify(self):
        await self.policy.refresh()
        self.save()
        self.store.deleted(CHANNEL,1)
        with patch('storage.time.time',return_value=time.time()+40):
            self.store.flush_digests()
        notify=next(job for job in self.store.pending() if job['kind']=='notify')
        self.store.set_channel_option(CHANNEL,'notifications_enabled',False)
        self.assertFalse(self.store.delivery_allowed(notify))
        self.assertTrue(any(self.store.delivery_allowed(job) for job in self.store.pending() if job['kind']=='archive'))

    async def test_partial_snapshot_does_not_unsubscribe_existing_channels(self):
        await self.policy.refresh()
        self.client.entities=[peer(456),TimeoutError()]
        with self.assertRaises(TimeoutError):
            await self.policy.refresh()
        self.assertTrue(self.store.channel_allowed(CHANNEL))
        self.assertIsNone(self.store.channel_state(-1000000000456))

    async def test_unknown_network_error_is_not_membership_and_is_rate_limited(self):
        self.client.request.side_effect=TimeoutError()
        self.assertFalse(await self.policy.admit(CHANNEL,peer()))
        self.assertFalse(await self.policy.admit(CHANNEL,peer()))
        self.client.request.assert_awaited_once()
        self.assertFalse(self.store.channel_allowed(CHANNEL))

    async def test_floodwait_pause_does_not_block_capture_handler(self):
        self.policy.network.paused_until=time.monotonic()+3600
        self.assertFalse(await asyncio.wait_for(self.policy.admit(CHANNEL,peer()),timeout=1))
        self.client.request.assert_not_awaited()

    async def test_channel_change_invalidates_cached_membership(self):
        await self.policy.refresh()
        await self.policy.raw_update(types.UpdateChannel(channel_id=123))
        self.assertFalse(self.store.channel_allowed(CHANNEL))
        self.client.request.side_effect=errors.UserNotParticipantError(request=None)
        self.assertFalse(await self.policy.admit(CHANNEL))

    async def test_stale_membership_fails_closed_and_auth_errors_propagate(self):
        await self.policy.refresh()
        with self.store.connection() as db:
            db.execute('UPDATE channel_settings SET verified_at=?',(time.time()-601,))
        self.assertFalse(self.store.channel_allowed(CHANNEL))
        self.client.request.side_effect=errors.AuthKeyUnregisteredError(request=None)
        with self.assertRaises(errors.AuthKeyUnregisteredError):
            await self.policy.admit(CHANNEL)

    async def test_regular_groups_and_private_chats_remain_available(self):
        group=types.Channel(id=999,title='Group',photo=types.ChatPhotoEmpty(),date=datetime.now(timezone.utc),megagroup=True)
        self.assertTrue(await self.policy.admit(-1000000000999,group))
        self.assertTrue(self.save(chat=-1000000000999))
        self.assertTrue(self.save(chat=42))
        self.assertTrue(self.save(chat=-123))

    async def test_username_blocks_both_only_for_joined_channels_and_paginates(self):
        await self.policy.refresh()
        for number in range(200,214):
            self.store.set_channel_membership(-1000000000000-number,True,title=f'Channel {number}')
        rows,page,count=self.store.channel_page(1)
        self.assertEqual((len(rows),page,count),(6,1,15))
        self.assertEqual(self.store.block_channel_username('FIXTURE_CHANNEL'),CHANNEL)
        row=self.store.channel_state(CHANNEL)
        self.assertFalse(row['capture_enabled'] or row['notifications_enabled'])
        self.store.set_channel_membership(CHANNEL,False,username='fixture_channel')
        self.assertIsNone(self.store.block_channel_username('fixture_channel'))

    async def test_restart_requires_subscription_revalidation(self):
        await self.policy.refresh()
        ChannelPolicy(self.client,Network(),self.store,Mock())
        self.assertFalse(self.store.channel_allowed(CHANNEL))

    async def test_real_capture_handlers_ignore_preview_messages_edits_and_deletions(self):
        channel = peer(left=True)
        client = Client()
        client.request.side_effect=errors.UserNotParticipantError(request=None)
        client.connect=AsyncMock();client.disconnect=AsyncMock()
        client.get_me=AsyncMock(return_value=NS(id=42))
        client.is_user_authorized=AsyncMock(return_value=True)
        client.set_receive_updates=AsyncMock()
        client.disconnected=asyncio.get_running_loop().create_future()
        handlers={}
        def register(builder):
            def decorate(fn):handlers[type(builder).__name__]=fn;return fn
            return decorate
        client.on=register
        message=NS(id=1,chat_id=CHANNEL,chat=channel,sender=None,sender_id=CHANNEL,
            media=None,post=True,date=datetime.now(timezone.utc),message='preview')
        async def catch_up():
            await handlers['NewMessage'](NS(chat_id=CHANNEL,message=message))
            await handlers['MessageEdited'](NS(chat_id=CHANNEL,message=message,id=1,raw_text='changed'))
            await handlers['MessageDeleted'](NS(chat_id=CHANNEL,deleted_ids=[1,2]))
            client.disconnected.set_result(None)
        client.catch_up=catch_up
        config=dict(DEFAULTS,api_id=12345,api_hash='a'*32,bot_token='fixture',log_chat_id='me',stories_enabled=False)
        (self.root/'media').mkdir()
        monitor=NS(refresh_contacts=AsyncMock(),contacts_refreshed=True)
        with patch('userbot.ROOT',self.root),patch('userbot.TelegramClient',return_value=client), \
             patch('userbot.read_config',return_value=config),patch('userbot.bind_owner'), \
             patch('userbot.StoryMonitor',return_value=monitor),patch('userbot.setup_logging',return_value=Mock()):
            await userbot.run()
        with self.store.connection() as db:
            for table in ('messages','deletion_signals','edits','outbox','media_jobs'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0,table)

    async def test_legacy_notification_metadata_still_respects_global_public_filter(self):
        await self.policy.refresh()
        with self.store.connection() as db:
            self.store.enqueue(db,f'digest:{CHANNEL}:legacy','notify','fixture',public_source=False)
        delivery=Delivery(self.store,NS(),Network(),dict(log_chat_id='me',delivery_interval_seconds=0),
            lambda:dict(admin_user_id=42,admin_chat_id=42,bot_token='fixture',notify_public_deletions=False),Mock())
        with patch('transport.requests.post') as post:
            await delivery.once()
        post.assert_not_called()

    async def test_bot_section_username_scope_exit_and_callback_authorisation(self):
        await self.policy.refresh()
        (self.root/'config.json').write_text(json.dumps(dict(bot_token='fixture',admin_user_id=42,
            admin_chat_id=42,notifications_enabled=True)),encoding='utf-8')
        store=self.store
        class Bot:
            def __init__(self):
                self.message_handlers=[];self.handlers={};self.sent=[];self.answered=[];self.edited=[]
            def message_handler(self,**kwargs):
                def register(fn):
                    self.message_handlers.append(fn);self.handlers[fn.__name__]=fn;return fn
                return register
            def callback_query_handler(self,**kwargs):
                def register(fn):self.callback=fn;return fn
                return register
            def send_message(self,*args,**kwargs):self.sent.append((args,kwargs))
            def edit_message_text(self,*args,**kwargs):self.edited.append((args,kwargs))
            def answer_callback_query(self,*args):self.answered.append(args)
            def stop_polling(self):pass
            def infinity_polling(bot,**kwargs):
                msg=NS(text='@fixture_channel',from_user=NS(id=42),chat=NS(id=42,type='private'),message_id=1)
                bot.handlers['handle'](msg)
                self.assertTrue(store.channel_state(CHANNEL)['capture_enabled'])
                msg.text='🎯 Отключить точечно';bot.handlers['handle'](msg)
                self.assertTrue(any((b.callback_data or '').startswith('channels:open:')
                    for row in bot.sent[-1][1]['reply_markup'].keyboard for b in row))
                msg.text='@FIXTURE_CHANNEL';bot.handlers['handle'](msg)
                self.assertFalse(store.channel_state(CHANNEL)['capture_enabled'])
                call=NS(id='callback',data=f'channels:save:{CHANNEL}:1:0',message=msg,from_user=NS(id=99))
                bot.callback(call)
                self.assertFalse(store.channel_state(CHANNEL)['capture_enabled'])
                call.from_user=NS(id=42);bot.callback(call)
                self.assertTrue(store.channel_state(CHANNEL)['capture_enabled'])
                self.assertFalse(store.channel_state(CHANNEL)['notifications_enabled'])
                self.assertTrue(bot.edited)
                call.data='channels:exit';bot.callback(call)
                msg.text='@fixture_channel';bot.handlers['handle'](msg)
                self.assertTrue(store.channel_state(CHANNEL)['capture_enabled'])
                self.assertIn('callback_query',kwargs['allowed_updates'])
        bot=Bot()
        with patch('telebot.TeleBot',return_value=bot),patch('runtime.ROOT',self.root),patch('notify_bot.ROOT',self.root),patch('notify_bot.setup_logging',return_value=Mock()):
            notify_bot.run_bot('fixture')
