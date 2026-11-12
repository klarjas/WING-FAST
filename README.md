# WING FAST

Telegram Mini App for ride booking in Cambodia.

## Included files

- `main.py` — FastAPI backend, Telegram WebApp authentication, SQLite database and role-based APIs.
- `index.html`, `style.css`, `app.js` — passenger, driver and admin Mini App interface.
- `requirements.txt` — pinned Python dependencies.

## Deploy

Use Python 3.12 or 3.13. Install dependencies:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Set these environment variables in the hosting dashboard (never commit secrets to GitHub):

- `BOT_TOKEN` — token from BotFather. Required for Telegram Mini App authentication.
- `ADMIN_IDS` — comma-separated Telegram numeric user IDs of trusted administrators.
- `DATABASE_PATH` — persistent SQLite file path. On a Render service with a mounted persistent disk, use a path such as `/var/data/wingfast.db`.
- `DEV_MODE` — leave unset or set to `false` in production. Only set `true` for isolated local testing without a bot token.

## Telegram setup

1. Create the WING FAST bot with BotFather.
2. Configure its Mini App URL to the deployed HTTPS service URL.
3. Set the same bot token in the host's `BOT_TOKEN` environment variable.
4. Add the administrator Telegram numeric IDs to `ADMIN_IDS`.
5. Open the Mini App from Telegram to authenticate. Direct public browser access is intentionally rejected in production.

## Important deployment notes

- Use HTTPS. Browser GPS requires a secure context and the passenger/driver must grant location permission.
- A SQLite database on an ephemeral container filesystem can be lost when the service restarts. Attach a persistent disk and set `DATABASE_PATH` to its mount path.
- Driver location is sent while the driver keeps the Mini App open and grants GPS permission. This is not Telegram's background Live Location feature.
- Digital payment is currently a payment-method selection only; a payment provider and verified payment callback must be integrated before accepting online payments.
- Fare calculation, map routing/geocoding, push notifications and production monitoring are not yet integrated.
- Do not enable `DEV_MODE` on a public service.
