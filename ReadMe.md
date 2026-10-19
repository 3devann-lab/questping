# QuestPing

A Discord bot and web dashboard that tells your server when a Meta Quest game gets a new build. Log in with Discord, pick a server, paste a store link, choose a channel and an optional role to ping.

QuestPing reads the public Meta store page in a headless browser. Meta has no public API for other people's games, so this is unofficial. If Meta changes the page, the reader in `store.py` may need a tweak, and a failed check shows up on the dashboard under the game. The store page does not expose developer builds, so QuestPing only reports public releases.

## What you get

- Discord login, no passwords stored
- Every server where you have Manage Server is listed automatically, with a button to add the bot
- Any number of servers and games, each with its own channel and role
- Checks every 15 minutes by default

## Set up Discord

1. Create an application at https://discord.com/developers/applications.
2. On the OAuth2 page, copy the Application ID and Client Secret, and add the redirect `YOUR_BASE_URL/callback`.
3. On the Bot page, reset and copy the token. Leave the privileged intents off.

Your running copy also shows these steps at `/setup`.

## Deploy on Railway

1. Fork this repo and create a new Railway project from it. The Dockerfile is picked up automatically.
2. Add a volume mounted at `/data`, otherwise your settings are lost on every deploy.
3. Generate a public domain, then set these variables:

| Variable | Value |
| --- | --- |
| `DISCORD_TOKEN` | the bot token |
| `DISCORD_CLIENT_ID` | the Application ID |
| `DISCORD_CLIENT_SECRET` | the Client Secret |
| `BASE_URL` | your public URL, for example `https://questping.up.railway.app` |
| `DATA_DIR` | `/data` |
| `CHECK_MINUTES` | optional, minimum 5, default 15 |

4. Add `BASE_URL/callback` as a redirect in the Discord portal, open your site, and log in.

Run a single instance. The bot and the website share one process.

## Run locally

```
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
set -a; . ./.env; set +a
hypercorn app:app --bind 127.0.0.1:8080
```

Use `http://127.0.0.1:8080` as `BASE_URL` and as the redirect.

## Security notes

- The bot token and client secret live only in environment variables.
- Discord tokens from login are used once to read your server list, then revoked.
- Sessions are random IDs stored server side, in `__Host-` cookies that are HttpOnly, Secure and SameSite Lax.
- Every change checks a CSRF token and the request origin, and re-checks live that you still have Manage Server in that server.
- Channels and roles are validated against the server, and pings can only mention the one chosen role.
- Store links are reduced to a numeric ID, so the server never fetches an address a user typed.
- Strict content security policy, rate limits, and a cap of 25 games per server.

## License

MIT
