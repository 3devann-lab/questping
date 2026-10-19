import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from functools import wraps
from urllib.parse import urlencode, urlparse

import discord
import httpx
from quart import Quart, abort, g, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException

import db
from bot import QuestBot
from store import parse_app_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("questping")
logging.getLogger("httpx").setLevel(logging.WARNING)


def need(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(name + " is not set")
    return value


BOT_TOKEN = need("DISCORD_TOKEN")
CLIENT_ID = need("DISCORD_CLIENT_ID")
CLIENT_SECRET = need("DISCORD_CLIENT_SECRET")
META_TOKEN = need("META_ACCESS_TOKEN")
BASE_URL = need("BASE_URL").rstrip("/")
DATA_DIR = os.environ.get("DATA_DIR", "data")
INTERVAL = max(5, int(os.environ.get("CHECK_MINUTES", "30")))

SECURE = BASE_URL.startswith("https://")
ORIGIN = "{0.scheme}://{0.netloc}".format(urlparse(BASE_URL))
REDIRECT_URI = BASE_URL + "/callback"
API = "https://discord.com/api/v10"
BOT_PERMISSIONS = 19456
MAX_TRACKERS = 25
SESSION_SECONDS = 7 * 24 * 3600
SESSION_COOKIE = "__Host-qp" if SECURE else "qp"
STATE_COOKIE = "__Host-qp-state" if SECURE else "qp-state"

NOTICES = {
    "added": "Tracking started. The first check runs in a moment, and nothing is posted for the current build.",
    "saved": "Changes saved.",
    "removed": "Game removed.",
    "bad_game": "That does not look like a Meta store link or app ID.",
    "bad_channel": "Pick a channel QuestPing can post in.",
    "bad_role": "That role is not available.",
    "exists": "That game is already tracked in this server.",
    "limit": "A server can track up to %d games." % MAX_TRACKERS,
}

ERRORS = {
    400: "That request could not be understood.",
    401: "Log in to continue.",
    403: "You do not have permission to do that.",
    404: "That page does not exist.",
    429: "Too many requests. Wait a minute and try again.",
}

app = Quart(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
bot = QuestBot(INTERVAL, META_TOKEN)
hits = {}


def limited(key, limit, window=60):
    now = asyncio.get_running_loop().time()
    if len(hits) > 5000:
        hits.clear()
    recent = [t for t in hits.get(key, []) if now - t < window]
    blocked = len(recent) >= limit
    if not blocked:
        recent.append(now)
    hits[key] = recent
    return blocked


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[-1].strip() or request.remote_addr or "unknown"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def same(a, b):
    return hmac.compare_digest(a.encode(), b.encode())


def safe_hash(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9_]{1,64}", value) else None


def clean_name(raw):
    return re.sub(r"[\x00-\x1f\x7f]", "", raw or "").strip()[:60]


def invite(gid=None):
    params = {"client_id": CLIENT_ID, "scope": "bot", "permissions": BOT_PERMISSIONS}
    if gid:
        params["guild_id"] = gid
        params["disable_guild_select"] = "true"
    return "https://discord.com/oauth2/authorize?" + urlencode(params)


def cookie(resp, name, value, age):
    resp.set_cookie(name, value, max_age=age, httponly=True, secure=SECURE, samesite="Lax", path="/")


def auth(view):
    @wraps(view)
    async def wrapper(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("home"))
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin != ORIGIN:
                abort(403)
            form = await request.form
            if not same(form.get("csrf", ""), g.user["csrf"]):
                abort(403)
        return await view(*args, **kwargs)

    return wrapper


async def manage_access(gid):
    if not re.fullmatch(r"\d{15,20}", gid):
        abort(404)
    if gid not in {item["id"] for item in json.loads(g.user["guilds"])}:
        abort(404)
    guild = bot.get_guild(int(gid))
    if guild is None:
        return None
    uid = int(g.user["user_id"])
    try:
        member = guild.get_member(uid) or await guild.fetch_member(uid)
    except discord.HTTPException:
        abort(403)
    perms = member.guild_permissions
    if not (guild.owner_id == uid or perms.administrator or perms.manage_guild):
        abort(403)
    if guild.me is None:
        abort(503)
    return guild


def find_channel(guild, raw):
    if not re.fullmatch(r"\d{15,20}", raw):
        return None
    channel = guild.get_channel(int(raw))
    if not isinstance(channel, discord.TextChannel):
        return None
    perms = channel.permissions_for(guild.me)
    return channel if perms.send_messages and perms.embed_links else None


def find_role(guild, raw):
    if not re.fullmatch(r"\d{15,20}", raw):
        return None
    role = guild.get_role(int(raw))
    if role is None or role.is_default() or role.managed:
        return None
    return role


def back(gid, code):
    return redirect(url_for("guild_page", gid=gid) + "?m=" + code, 303)


@app.before_serving
async def start():
    await db.connect(os.path.join(DATA_DIR, "questping.db"))

    def done(task):
        if not task.cancelled() and task.exception():
            log.error("bot stopped: %s", task.exception())

    app.bot_task = asyncio.create_task(bot.start(BOT_TOKEN))
    app.bot_task.add_done_callback(done)


@app.after_serving
async def stop():
    await bot.close()
    await db.close()


@app.before_request
async def load_session():
    g.user = None
    if request.path.startswith("/static"):
        return
    if limited("all:" + client_ip(), 240):
        abort(429)
    if request.method == "POST" and limited("post:" + client_ip(), 40):
        abort(429)
    raw = request.cookies.get(SESSION_COOKIE, "")
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", raw):
        g.user = await db.get_session(digest(raw))


@app.after_request
async def harden(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' https://cdn.discordapp.com; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if SECURE:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if not request.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.context_processor
async def inject():
    return {"user": getattr(g, "user", None)}


@app.errorhandler(HTTPException)
async def http_error(err):
    code = err.code or 500
    return await render_template("error.html", code=code, text=ERRORS.get(code, "Something went wrong.")), code


@app.get("/")
async def home():
    return await render_template("home.html")


@app.get("/setup")
async def setup():
    return await render_template("setup.html", redirect_uri=REDIRECT_URI, invite=invite())


@app.get("/login")
async def login():
    if limited("login:" + client_ip(), 15):
        abort(429)
    state = secrets.token_urlsafe(32)
    query = urlencode(
        {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "identify guilds",
            "state": state,
        }
    )
    resp = redirect("https://discord.com/oauth2/authorize?" + query)
    cookie(resp, STATE_COOKIE, state, 600)
    return resp


@app.get("/callback")
async def callback():
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    expected = request.cookies.get(STATE_COOKIE, "")
    if not expected or not same(state, expected) or not code or len(code) > 200:
        abort(400)
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            token_reply = await http.post(
                API + "/oauth2/token",
                data={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                },
            )
            if token_reply.status_code != 200:
                abort(400)
            token = token_reply.json()["access_token"]
            headers = {"Authorization": "Bearer " + token}
            me = await http.get(API + "/users/@me", headers=headers)
            owned = await http.get(API + "/users/@me/guilds", headers=headers)
            await http.post(
                API + "/oauth2/token/revoke",
                data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "token": token},
            )
    except (httpx.HTTPError, KeyError, ValueError):
        abort(502)
    if me.status_code != 200 or owned.status_code != 200:
        abort(502)
    profile = me.json()
    if not re.fullmatch(r"\d{15,20}", str(profile.get("id", ""))):
        abort(502)
    guilds = []
    for item in owned.json():
        perms = int(item.get("permissions", "0"))
        if re.fullmatch(r"\d{15,20}", str(item.get("id", ""))) and (item.get("owner") or perms & 0x8 or perms & 0x20):
            guilds.append({"id": item["id"], "name": str(item.get("name", ""))[:100], "icon": safe_hash(item.get("icon"))})
    user = {
        "id": profile["id"],
        "name": str(profile.get("global_name") or profile.get("username") or "user")[:80],
        "avatar": safe_hash(profile.get("avatar")),
    }
    sid = secrets.token_urlsafe(32)
    await db.create_session(digest(sid), user, guilds, secrets.token_urlsafe(32), SESSION_SECONDS)
    resp = redirect(url_for("dashboard"))
    cookie(resp, SESSION_COOKIE, sid, SESSION_SECONDS)
    resp.delete_cookie(STATE_COOKIE, path="/")
    return resp


@app.post("/logout")
@auth
async def logout():
    await db.delete_session(g.user["id"])
    resp = redirect(url_for("home"), 303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/dashboard")
@auth
async def dashboard():
    counts = await db.tracker_counts()
    rows = []
    for item in json.loads(g.user["guilds"]):
        rows.append(
            {
                **item,
                "installed": bot.get_guild(int(item["id"])) is not None,
                "trackers": counts.get(item["id"], 0),
                "invite": invite(item["id"]),
            }
        )
    rows.sort(key=lambda row: (not row["installed"], row["name"].lower()))
    return await render_template("dashboard.html", guilds=rows)


@app.get("/guild/<gid>")
@auth
async def guild_page(gid):
    guild = await manage_access(gid)
    if guild is None:
        return redirect(invite(gid))
    channels = [c for c in guild.text_channels if find_channel(guild, str(c.id))]
    roles = [r for r in reversed(guild.roles) if not r.is_default() and not r.managed]
    return await render_template(
        "guild.html",
        guild=guild,
        channels=channels,
        roles=roles,
        trackers=await db.trackers_for_guild(gid),
        notice=NOTICES.get(request.args.get("m", "")),
    )


@app.post("/guild/<gid>/add")
@auth
async def add_tracker(gid):
    guild = await manage_access(gid)
    if guild is None:
        abort(404)
    form = await request.form
    app_id = parse_app_id(form.get("game", ""))
    if app_id is None:
        return back(gid, "bad_game")
    channel = find_channel(guild, form.get("channel", ""))
    if channel is None:
        return back(gid, "bad_channel")
    role = None
    if form.get("role"):
        role = find_role(guild, form["role"])
        if role is None:
            return back(gid, "bad_role")
    if await db.count_trackers(gid) >= MAX_TRACKERS:
        return back(gid, "limit")
    name = clean_name(form.get("name", ""))
    if not await db.add_tracker(
        gid, app_id, name, str(channel.id), str(role.id) if role else None, form.get("dev") == "on"
    ):
        return back(gid, "exists")
    bot.spawn(bot.check_app(app_id))
    return back(gid, "added")


@app.post("/guild/<gid>/tracker/<int:tid>")
@auth
async def update_tracker(gid, tid):
    guild = await manage_access(gid)
    if guild is None:
        abort(404)
    form = await request.form
    channel = find_channel(guild, form.get("channel", ""))
    if channel is None:
        return back(gid, "bad_channel")
    role = None
    if form.get("role"):
        role = find_role(guild, form["role"])
        if role is None:
            return back(gid, "bad_role")
    await db.update_tracker(
        tid,
        gid,
        clean_name(form.get("name", "")),
        str(channel.id),
        str(role.id) if role else None,
        form.get("dev") == "on",
    )
    return back(gid, "saved")


@app.post("/guild/<gid>/tracker/<int:tid>/delete")
@auth
async def delete_tracker(gid, tid):
    if await manage_access(gid) is None:
        abort(404)
    await db.delete_tracker(tid, gid)
    return back(gid, "removed")
