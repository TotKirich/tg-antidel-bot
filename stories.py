"""Monitor accessible stories of all contacts without marking them as viewed."""
import asyncio
import sqlite3
import time

from telethon import functions, types, utils
from transport import AUTH_ERRORS, media_bytes


class StoryMonitor:
    def __init__(self, client, network, store, logger, poll_seconds=300):
        self.client, self.network, self.store = client, network, store
        self.logger, self.poll_seconds = logger, poll_seconds
        self.contacts = set()
        self.contacts_refreshed = 0

    async def refresh_contacts(self):
        result = await self.network.call(lambda: self.client(functions.contacts.GetContactsRequest(hash=0)))
        self.contacts = {contact.user_id for contact in result.contacts}
        names = [(user.id,getattr(user,'first_name',None),getattr(user,'last_name',None),getattr(user,'username',None))
                 for user in result.users if user.id in self.contacts]
        await asyncio.to_thread(self.store.replace_contacts,names)
        # Telethon caches returned users; explicitly provide them for subsequent peer resolution.
        for user in result.users:
            await self.client.get_input_entity(user)
        self.contacts_refreshed = time.monotonic()

    async def ingest(self, owner_id, story, *, explicit=False):
        if owner_id not in self.contacts:
            return
        if isinstance(story, types.StoryItemDeleted):
            self.store.story_removed(owner_id, story.id, explicit=explicit)
        elif isinstance(story, types.StoryItem) and not story.min:
            self.store.story(owner_id, story.id, story.caption or '',
                story.date.timestamp(), story.expire_date.timestamp(),
                media_payload=media_bytes(story.media) if story.expire_date.timestamp() > time.time() else None)
        # Skipped/minimal objects and missing pages do not prove deletion.

    async def raw_update(self, update):
        if isinstance(update, types.UpdateStory):
            await self.ingest(utils.get_peer_id(update.peer), update.story, explicit=True)

    async def scan(self):
        if not self.contacts_refreshed or time.monotonic() - self.contacts_refreshed >= 3600:
            await self.refresh_contacts()
        # Start a complete snapshot each round. A state-only incremental request does
        # not guarantee discovery of new stories posted by an already listed peer.
        for hidden in (False, True):
            state = None
            seen_states = set()
            while True:
                result = await self.network.call(lambda: self.client(functions.stories.GetAllStoriesRequest(
                    hidden=hidden, state=state, next=True if state else None)))
                if isinstance(result, types.stories.AllStoriesNotModified):
                    break
                for peer_stories in result.peer_stories:
                    owner_id = utils.get_peer_id(peer_stories.peer)
                    skipped = []
                    for story in peer_stories.stories:
                        if isinstance(story, types.StoryItemSkipped) or getattr(story, 'min', False):
                            skipped.append(story.id)
                        else:
                            await self.ingest(owner_id, story)
                    if skipped and owner_id in self.contacts:
                        await self.fetch_ids(owner_id, skipped)
                if not result.has_more:
                    break
                if result.state in seen_states:
                    raise RuntimeError('Repeated stories pagination cursor')
                seen_states.add(result.state)
                state = result.state
        # Only query previously seen, still-live IDs. Absence from the contact feed
        # or RPC errors never becomes a deletion signal.
        grouped = {}
        for row in self.store.active_stories():
            if row['owner_id'] in self.contacts:
                grouped.setdefault(row['owner_id'], []).append(row['story_id'])
        for owner_id, ids in grouped.items():
            await self.fetch_ids(owner_id, ids)
        self.store.heartbeat('stories_scan_ok', time.time())

    async def fetch_ids(self, owner_id, ids):
        try:
            peer = await self.client.get_input_entity(owner_id)
            for offset in range(0, len(ids), 100):
                result = await self.network.call(lambda: self.client(functions.stories.GetStoriesByIDRequest(
                    peer=peer, id=ids[offset:offset + 100])))
                for story in result.stories:
                    await self.ingest(owner_id, story)
                await asyncio.sleep(0.1)
        except (*AUTH_ERRORS, sqlite3.Error):
            raise
        except Exception as error:
            self.logger.warning('Story peer unavailable id=%s type=%s', owner_id, type(error).__name__)

    async def run(self):
        while True:
            try:
                await self.scan()
            except (*AUTH_ERRORS, sqlite3.Error):
                raise
            except Exception as error:
                self.logger.warning('Story scan failed type=%s; no deletion inferred', type(error).__name__)
                self.store.heartbeat('stories_error', type(error).__name__)
            await asyncio.sleep(self.poll_seconds)
