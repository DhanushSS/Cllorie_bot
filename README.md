# Telegram Calorie & Protein Tracker Bot

A personal Telegram bot that pings you at morning/afternoon/night, asks what you ate, estimates
calories & protein with an NVIDIA-hosted LLM, logs everything to a Google Sheet, and gives you
today/week/month reports with charts — right inside Telegram.

## How it behaves

- Three daily check-ins (times configurable). It won't ask about the next meal until the current
  one is logged **or** skipped.
- If a dish is ambiguous (biryani, curry, sandwich, etc.) it asks "veg or non-veg?" with quick
  tap buttons, then finishes the estimate.
- `/skip` skips whatever meal is currently pending — e.g. wake up late, skip breakfast, and
  immediately log lunch.
- `/edit` lets you correct any meal you already logged today (overwrites that row in the sheet).
- `/today`, `/week`, `/month` give you a text summary plus a calories+protein chart.
- Meal logs are stored durably in Google Sheets (viewable/editable from your phone anytime).
  A small local SQLite file just tracks "which meal is pending right now" — if it's ever wiped by
  a redeploy, worst case today's flow just resets, your logged data is untouched.

## 1. Create the Telegram bot

1. Message **@BotFather** on Telegram → `/newbot` → follow the prompts.
2. Copy the token it gives you (looks like `123456789:AA...`).

## 2. Get an NVIDIA NIM API key

1. Go to https://build.nvidia.com and sign in.
2. Get an API key (starts with `nvapi-`) — NVIDIA gives free credits for their hosted models.
3. Pick a solid instruction-following chat model, e.g. `meta/llama-3.3-70b-instruct` (already set
   as the default in `.env.example`). You can browse other available chat models on the site and
   swap `NVIDIA_MODEL` if you prefer another.

## 3. Set up the Google Sheet

1. Create a new Google Sheet (any name). Grab the **Sheet ID** from its URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`
2. In Google Cloud Console: create a project → enable the **Google Sheets API** → create a
   **Service Account** → create a JSON key for it and download it.
3. Open the downloaded JSON, find `client_email`, and **share your Google Sheet** with that email
   address as an **Editor**.
4. Paste the entire JSON file's contents as a single line into `GOOGLE_SERVICE_ACCOUNT_JSON` in
   your `.env`.

## 4. Run it locally first (recommended)

```bash
cd telegram-calorie-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# now fill in .env with your real values
python bot.py
```

Message your bot `/start` on Telegram. It should greet you and start scheduling reminders.
Test `/skip`, `/edit`, `/today` etc. Once it feels right, deploy it so it survives your laptop
being off.

## 5. Deploy to the cloud (always-on)

This bot uses **polling** (not webhooks), so it just needs to be a long-running process — no
public URL/port required. A couple of good free/cheap options:

### Option A: Railway (simplest)

1. Push this folder to a GitHub repo.
2. On https://railway.app → New Project → Deploy from GitHub repo → pick your repo. Railway
   auto-detects the `Dockerfile`.
3. In the project's **Variables** tab, add every key from your `.env` (paste the JSON for
   `GOOGLE_SERVICE_ACCOUNT_JSON` as one line, same as locally).
4. Deploy. Check the **Deployments → Logs** tab for `Bot starting (polling)...`.
5. Message `/start` to your bot from your phone — it now runs independent of your laptop.

   Note: Railway's free trial credit runs out and then bills small usage-based amounts
   (this bot is lightweight — should be low single-digit dollars a month at most). Check their
   current pricing page before committing.

### Option B: Fly.io

1. Install `flyctl`, run `fly launch` inside this folder (it'll detect the Dockerfile — say no to
   a Postgres/Redis add-on, you don't need one).
2. Set secrets: `fly secrets set TELEGRAM_BOT_TOKEN=... NVIDIA_API_KEY=... GOOGLE_SHEET_ID=... GOOGLE_SHEET_TAB=Logs TIMEZONE=Asia/Kolkata GOOGLE_SERVICE_ACCOUNT_JSON='...'`
3. `fly deploy`. Fly's small free-tier VM allowance is normally enough for one always-on
   lightweight worker like this — check their current free-tier terms, they do change.

### Option C: Render

Use a **Background Worker** service (not a Web Service — web services on Render's free tier spin
down on inactivity, which would silently kill your reminders). Point it at this repo with build
command `pip install -r requirements.txt` and start command `python bot.py`, and add the same
environment variables.

## Files

| File | Purpose |
|---|---|
| `bot.py` | Telegram handlers, scheduling, conversation flow |
| `config.py` | Loads all settings from environment variables |
| `db.py` | SQLite: tracks which meal is currently pending, per day |
| `nvidia_client.py` | Calls the NVIDIA NIM API to estimate calories/protein, flags ambiguous dishes |
| `sheets_client.py` | Reads/writes the Google Sheet |
| `charts.py` | Builds the calorie/protein chart + text summaries for reports |

## Customizing

- Change reminder times: edit `REMINDER_MORNING` / `REMINDER_AFTERNOON` / `REMINDER_NIGHT` in
  your env (24h `HH:MM`, in the `TIMEZONE` you set).
- Change the estimation model: edit `NVIDIA_MODEL`.
- Add a 4th meal slot (e.g. snacks): duplicate the `morning`/`Breakfast` pattern in
  `config.MEALS`/`MEAL_LABELS` and `schedule_reminders`.
