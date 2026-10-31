"""Subscription checks for broadcast channels; groups retain their existing scope."""
import asyncio
import time

from telethon import errors, functions, types, utils
from transport import AUTH_ERRORS


class ChannelPolicy:
    def __init__(self, client, network, store, logger):
        self.client, self.network, self.store, self.logger = client, network, store, logger
        self.attempts = {}
        store.activate_channel_policy()

    async def refresh(self):
        # Commit only a complete snapshot. A failed page must not imply a leave.
        peers = []
        iterator = self.client.iter_dialogs(ignore_migrated=True).__aiter__()
        while True:
            try:
                dialog = await self.network.call(iterator.__anext__)
            except StopAsyncIteration:
                break
            peer = dialog.entity
            if isinstance(peer, types.Channel) and not getattr(peer, 'left', False) and not getattr(peer, 'deactivated', False):
                peers.append(dict(chat_id=utils.get_peer_id(peer), title=peer.title,
                                  username=peer.username or '', is_broadcast=not bool(peer.megagroup)))
        await asyncio.to_thread(self.store.sync_channels, peers)

    async def admit(self, chat_id, peer=None):
        if chat_id is None or chat_id > -1000000000000:
            return True
        state = self.store.channel_state(chat_id)
        if isinstance(peer, types.Channel) and getattr(peer, 'left', False):
            self.store.set_channel_membership(chat_id, False, is_broadcast=not bool(peer.megagroup))
            return bool(peer.megagroup)
        if state and not state['is_broadcast']:
            return True
        if state and self.store.channel_allowed(chat_id):
            return True
        # A local capture opt-out never triggers a membership RPC per message.
        if state and not state['capture_enabled']:
            return False
        if time.monotonic() < self.network.paused_until:
            return False
        now = time.monotonic()
        if now - self.attempts.get(chat_id, -60) < 60:
            return False
        if len(self.attempts) > 1024:
            self.attempts.clear()
        self.attempts[chat_id] = now
        try:
            peer = peer or await self.network.call(lambda: self.client.get_entity(chat_id), timeout=5)
            if not isinstance(peer, types.Channel):
                return False
            if peer.megagroup:
                self.store.set_channel_membership(chat_id, True, is_broadcast=False)
                return True
            result = await self.network.call(lambda: self.client(functions.channels.GetParticipantRequest(
                channel=peer, participant=types.InputPeerSelf())), timeout=5)
            joined = isinstance(result.participant, (types.ChannelParticipant, types.ChannelParticipantSelf,
                types.ChannelParticipantCreator, types.ChannelParticipantAdmin))
            self.store.set_channel_membership(chat_id, joined, title=peer.title, username=peer.username or '')
            return joined and self.store.channel_allowed(chat_id)
        except (errors.UserNotParticipantError, errors.ChannelPrivateError):
            self.store.set_channel_membership(chat_id, False)
        except AUTH_ERRORS:
            raise
        except Exception as error:
            self.logger.warning('Channel subscription check pending type=%s', type(error).__name__)
        return False

    async def raw_update(self, update):
        if isinstance(update, types.UpdateChannel):
            chat_id = -1000000000000 - update.channel_id
            # Do not trust a cached joined flag after a subscription change.
            self.store.invalidate_channel(chat_id)
            self.attempts.pop(chat_id, None)

    async def run(self):
        while True:
            try:
                await self.refresh()
            except AUTH_ERRORS:
                raise
            except Exception as error:
                self.logger.warning('Channel subscription sync pending type=%s', type(error).__name__)
            await asyncio.sleep(300)
