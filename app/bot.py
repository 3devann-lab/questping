import asyncio
import logging
import random

import discord

import db
from store import Store, StoreError

log = logging.getLogger("questping")

ACCENT = 0x5B4DFF


class QuestBot(discord.Client):
    def __init__(self, interval, token):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.interval = interval
        self.store = Store(token)
        self.tasks = set()
        self.busy = set()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def setup_hook(self):
        self.spawn(self.loop_forever())

    async def on_guild_remove(self, guild):
        await db.delete_guild(str(guild.id))

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await self.store.close()
        await super().close()

    async def loop_forever(self):
        await self.wait_until_ready()
        while True:
            try:
                for app_id in await db.all_app_ids():
                    await self.check_app(app_id)
                    await asyncio.sleep(random.uniform(1, 3))
                await db.purge_sessions()
            except Exception:
                log.exception("check cycle failed")
            await asyncio.sleep(self.interval * 60)

    async def check_app(self, app_id):
        if app_id in self.busy:
            return
        self.busy.add(app_id)
        try:
            try:
                release = await self.store.fetch(app_id)
            except StoreError as err:
                log.warning("check failed for %s: %s", app_id, err)
                await db.set_game_error(app_id, str(err)[:200])
                return
            await db.set_game(app_id, release.live, release.dev)
            for tracker in await db.trackers_for_app(app_id):
                await self.settle(tracker, release)
        finally:
            self.busy.discard(app_id)

    async def settle(self, tracker, release):
        version = tracker["last_version"]
        dev = tracker["last_dev"]
        if release.live is not None:
            if version is None:
                version = release.live
            elif version != release.live:
                if await self.announce(tracker, release.live, version, False):
                    version = release.live
        key = release.dev or "-"
        if dev is None:
            dev = key
        elif dev != key:
            if release.dev and tracker["dev_builds"]:
                if await self.announce(tracker, release.dev, dev, True):
                    dev = key
            else:
                dev = key
        if (version, dev) != (tracker["last_version"], tracker["last_dev"]):
            await db.set_marks(tracker["id"], version, dev)

    async def announce(self, tracker, label, old, is_dev):
        channel = self.get_channel(int(tracker["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return False
        perms = channel.permissions_for(channel.guild.me)
        if not (perms.send_messages and perms.embed_links):
            return False
        name = tracker["name"] or "App " + tracker["app_id"]
        embed = discord.Embed(
            title=name[:150] + (" has a new developer build" if is_dev else " has a new build"),
            url="https://www.meta.com/experiences/view/" + tracker["app_id"] + "/",
            colour=ACCENT,
        )
        if old and old != "-":
            embed.add_field(name="Previous", value=old[:200])
        embed.add_field(name="Now", value=label[:200])
        if is_dev:
            embed.set_footer(text="Developer build, not on a public channel")
        role = tracker["role_id"]
        mentions = discord.AllowedMentions(
            everyone=False, users=False, roles=[discord.Object(id=int(role))] if role else False
        )
        try:
            await channel.send(content=("<@&" + role + ">") if role else None, embed=embed, allowed_mentions=mentions)
        except discord.HTTPException:
            log.exception("could not post in channel %s", channel.id)
            return False
        return True
