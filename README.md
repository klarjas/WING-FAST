# WING FAST

Telegram Mini App for ride booking in Cambodia.

## Project files

- `main.py` — FastAPI backend, Telegram WebApp authentication, Supabase PostgreSQL database and role-based APIs.
- `index.html`, `style.css`, `app.js` — passenger, driver and admin interface.
- `requirements.txt` — pinned Python dependencies.
- `.python-version` — Render Python runtime pin.

## Render deployment

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Set these environment variables in Render Dashboard (never commit secrets to GitHub):

- `BOT_TOKEN` — Telegram bot token from BotFather.
- `ADMIN_IDS` — comma-separated Telegram numeric IDs of trusted administrators.
- `SUPABASE_DB_URL` — Supabase PostgreSQL connection string. Use the Supabase Session Pooler connection string with SSL enabled.

The backend requires `SUPABASE_DB_URL`; it does not create tables automatically. Run the WING FAST database SQL in the Supabase SQL Editor before deploying.

## Telegram setup

1. Create the WING FAST bot with BotFather.
2. Set the Mini App URL to the deployed HTTPS Render URL.
3. Add the bot token to Render as `BOT_TOKEN`.
4. Add trusted Telegram numeric IDs to `ADMIN_IDS`.
5. Open the Mini App from Telegram.

## Important notes

- Render Free has no persistent disk; Supabase PostgreSQL is used for persistent application data.
- Browser GPS requires HTTPS and user permission.
- Driver GPS updates are sent while the Mini App remains open; this is not background tracking.
- Digital payment is currently a payment-method selection only. No payment gateway is connected.
- Fare calculation, geocoding, map routing, push notifications and production monitoring are not yet integrated.
- Never expose the Supabase database password or connection string in GitHub or chat.


## Vehicle photo update

Before deploying the vehicle-photo update, run migrations/20261113_vehicle_photos.sql once in the Supabase SQL Editor. This adds the vehicle photo media reference used by driver registration and the admin driver review screen. Vehicle images are stored in the configured Telegram storage channel through the existing /api/media endpoint; the database stores only the media reference.


## Vehicle category display images and last page

Run migrations/20261113_vehicle_type_images.sql once in the Supabase SQL Editor. The Admin panel then lets administrators upload or replace the default SUV, Sedan, Tuk-Tuk, Motorcycle and Van images shown on the Ride screen. The last visited app page and chosen vehicle type are saved locally on the user's device.
