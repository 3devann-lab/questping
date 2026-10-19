import json
import os
import time

import aiosqlite

SCHEMA = """
create table if not exists sessions (
    id text primary key,
    user_id text not null,
    username text not null,
    avatar text,
    guilds text not null,
    csrf text not null,
    expires integer not null
);
create table if not exists games (
    app_id text primary key,
    version text,
    dev text,
    checked integer,
    error text
);
create table if not exists trackers (
    id integer primary key autoincrement,
    guild_id text not null,
    app_id text not null,
    name text,
    channel_id text not null,
    role_id text,
    dev_builds integer not null default 0,
    last_version text,
    last_dev text,
    unique (guild_id, app_id)
);
create index if not exists trackers_guild on trackers (guild_id);
"""

conn = None


async def connect(path):
    global conn
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("pragma journal_mode = wal")
    await conn.executescript(SCHEMA)
    await conn.commit()


async def close():
    if conn is not None:
        await conn.close()


async def one(sql, args=()):
    async with conn.execute(sql, args) as cur:
        return await cur.fetchone()


async def many(sql, args=()):
    async with conn.execute(sql, args) as cur:
        return await cur.fetchall()


async def run(sql, args=()):
    async with conn.execute(sql, args) as cur:
        await conn.commit()
        return cur.rowcount


async def create_session(sid, user, guilds, csrf, ttl):
    await run(
        "insert into sessions values (?, ?, ?, ?, ?, ?, ?)",
        (sid, user["id"], user["name"], user["avatar"], json.dumps(guilds), csrf, int(time.time()) + ttl),
    )


async def get_session(sid):
    return await one("select * from sessions where id = ? and expires > ?", (sid, int(time.time())))


async def delete_session(sid):
    await run("delete from sessions where id = ?", (sid,))


async def purge_sessions():
    await run("delete from sessions where expires <= ?", (int(time.time()),))


async def trackers_for_guild(gid):
    return await many(
        "select t.id, t.app_id, t.name, t.channel_id, t.role_id, t.dev_builds, g.version, g.dev, g.error "
        "from trackers t left join games g on g.app_id = t.app_id "
        "where t.guild_id = ? order by t.id",
        (gid,),
    )


async def trackers_for_app(app_id):
    return await many("select * from trackers where app_id = ?", (app_id,))


async def tracker_counts():
    rows = await many("select guild_id, count(*) as n from trackers group by guild_id")
    return {row["guild_id"]: row["n"] for row in rows}


async def count_trackers(gid):
    row = await one("select count(*) as n from trackers where guild_id = ?", (gid,))
    return row["n"]


async def add_tracker(gid, app_id, name, channel_id, role_id, dev_builds):
    known = await one("select version, dev, checked from games where app_id = ?", (app_id,))
    seen = known is not None and known["checked"] is not None
    added = await run(
        "insert or ignore into trackers "
        "(guild_id, app_id, name, channel_id, role_id, dev_builds, last_version, last_dev) "
        "values (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            gid,
            app_id,
            name or None,
            channel_id,
            role_id,
            int(dev_builds),
            known["version"] if seen else None,
            (known["dev"] or "-") if seen else None,
        ),
    )
    return added == 1


async def update_tracker(tid, gid, name, channel_id, role_id, dev_builds):
    await run(
        "update trackers set name = ?, channel_id = ?, role_id = ?, dev_builds = ? where id = ? and guild_id = ?",
        (name or None, channel_id, role_id, int(dev_builds), tid, gid),
    )


async def delete_tracker(tid, gid):
    await run("delete from trackers where id = ? and guild_id = ?", (tid, gid))


async def delete_guild(gid):
    await run("delete from trackers where guild_id = ?", (gid,))


async def all_app_ids():
    rows = await many("select distinct app_id from trackers")
    return [row["app_id"] for row in rows]


async def set_marks(tid, version, dev):
    await run("update trackers set last_version = ?, last_dev = ? where id = ?", (version, dev, tid))


async def set_game(app_id, version, dev):
    await run(
        "insert into games (app_id, version, dev, checked, error) values (?, ?, ?, ?, null) "
        "on conflict (app_id) do update set "
        "version = excluded.version, dev = excluded.dev, checked = excluded.checked, error = null",
        (app_id, version, dev, int(time.time())),
    )


async def set_game_error(app_id, error):
    await run(
        "insert into games (app_id, error) values (?, ?) "
        "on conflict (app_id) do update set error = excluded.error",
        (app_id, error),
    )
