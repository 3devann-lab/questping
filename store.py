import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

GRAPH = "https://graph.oculus.com/graphql"
HISTORY_DOC = "2885322071572384"
HOSTS = {"meta.com", "www.meta.com", "oculus.com", "www.oculus.com"}


class StoreError(Exception):
    pass


@dataclass
class Build:
    version: str
    code: int
    channels: list

    @property
    def label(self):
        if self.version:
            return "%s (build %d)" % (self.version, self.code)
        return "build %d" % self.code


@dataclass
class Release:
    live: str
    dev: str


def parse_app_id(raw):
    raw = (raw or "").strip()
    if re.fullmatch(r"\d{8,20}", raw):
        return raw
    if len(raw) > 300:
        return None
    try:
        url = urlparse(raw)
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or url.hostname not in HOSTS:
        return None
    if not url.path.startswith("/experiences/"):
        return None
    found = re.findall(r"/(\d{8,20})(?=/|$)", url.path)
    return found[-1] if found else None


def find(node, key):
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            hit = find(value, key)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for value in node:
            hit = find(value, key)
            if hit is not None:
                return hit
    return None


def channel_names(node):
    names = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("channel_name", "name") and isinstance(value, str):
                names.append(value)
            else:
                names += channel_names(value)
    elif isinstance(node, list):
        for value in node:
            names += channel_names(value)
    return names


def read_builds(data):
    errors = data.get("errors") if isinstance(data, dict) else None
    if errors and isinstance(errors, list) and isinstance(errors[0], dict):
        raise StoreError("Meta said: " + str(errors[0].get("message", "unknown error"))[:80])
    binaries = find(data, "primary_binaries")
    nodes = binaries.get("nodes") if isinstance(binaries, dict) else None
    if not nodes:
        raise StoreError("no builds in the reply, check the app ID")
    builds = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        try:
            code = int(node.get("version_code"))
        except (TypeError, ValueError):
            continue
        builds.append(Build(str(node.get("version") or ""), code, channel_names(node.get("binary_release_channels"))))
    if not builds:
        raise StoreError("the reply held no readable builds")
    released = [b for b in builds if b.channels]
    public = [b for b in released if any(c.upper() == "LIVE" for c in b.channels)] or released
    live = max(public, key=lambda b: b.code) if public else None
    top = max(builds, key=lambda b: b.code)
    ceiling = max((b.code for b in released), default=0)
    dev = top if top.code > ceiling else None
    return Release(live.label if live else None, dev.label if dev else None)


class Store:
    def __init__(self, token=None):
        # The token is optional. Meta's public store endpoint answers for any
        # app ID without an account, so it is only sent when one is configured.
        self.token = token or None
        self.http = httpx.AsyncClient(timeout=30)
        self.gate = asyncio.Semaphore(3)

    async def fetch(self, app_id):
        params = {
            "doc_id": HISTORY_DOC,
            "variables": json.dumps({"applicationID": app_id}),
        }
        if self.token:
            params["access_token"] = self.token
        async with self.gate:
            try:
                reply = await self.http.get(GRAPH, params=params)
            except httpx.HTTPError:
                raise StoreError("could not reach Meta") from None
        if reply.status_code != 200:
            raise StoreError("Meta answered with status %d" % reply.status_code)
        try:
            data = reply.json()
        except ValueError:
            raise StoreError("Meta sent a reply that is not JSON") from None
        return read_builds(data)

    async def close(self):
        await self.http.aclose()
