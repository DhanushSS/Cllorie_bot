# Cllorie Bot

Personal Telegram calorie and protein diary, hosted on Azure with outbound polling.
Meals are recorded in Google Sheets; SQLite preserves ownership, meal state, pending
clarifications and raw food estimates across deployments. Default timezone is Asia/Kolkata.

## Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Register the first owner and show help; optional allowed chat restriction |
| `/test [food]` | Try the same estimation pipeline without saving meals, estimates or usuals |
| `/cancel` | Leave test mode or cancel a pending question |
| `/usuals` | Preview your saved usual meals before logging |
| `/remember name`, `/forget name` | Explicitly save or remove a nickname for your last logged meal |
| `/skip` | Skip the earliest pending meal and unlock the next meal |
| `/edit` | Replace one of today's logged meals, including after clarification |
| `/fix morning 650 25` | Set breakfast's final totals to 650 kcal and 25 g protein; update its cache |
| `/today` | Today's text summary |
| `/week`, `/month` | Last 7 / 30 days text summary and chart when data exists |

Only the registered owner can read reports or change records. Meals must be completed
or skipped in order: morning, afternoon, night. Ambiguous foods get a relevant type or portion
question, with answer buttons when available. A separate portion check prevents
common variable-serving dishes (rice, biryani, curry, dal and others) from being
saved on an automatic guess when the user has supplied no recognized amount.
It ignores amounts appearing only in the bot's own questions. Estimates are approximate and depend on the described portion.

The cache is checked first. For uncached foods the AI resolves ambiguity, then usable
API Ninjas nutrients take priority unless database calories are **strictly below**
`NUTRITION_DB_MIN_RATIO × AI calories`. In that case the AI is used with the note
“AI estimate: portion-adjusted.” At equality the database wins. Missing or placeholder
nutrients fall back to AI; unusable AI and database results are not logged or cached.
Raw calories are cached; the calorie multiplier applies once when saving/displaying.
`/fix` accepts final calories and divides them by the multiplier for the cache.

API Ninjas currently marks calories and protein as premium fields. The three live
checks on 18 October 2026 returned premium-only placeholders, so this deployment can
leave its optional key empty and use NVIDIA plus the cache. No paid upgrade is needed.
Source: https://api-ninjas.com/api/nutrition

## Configuration

Copy `.env.example` to `.env` and edit it privately. Never paste credentials in chat,
shell arguments or git. Google JSON must be on one line. Docker's env-file format uses
literal values: do not wrap the JSON or other values in outer shell quotes.

| Variable | Default / purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Required, replacement token from BotFather |
| `NVIDIA_API_KEY` | Required NVIDIA API key |
| `NVIDIA_THINKING` | `true`, reasoning enabled |
| `NVIDIA_TIMEOUT_SECONDS` | `240`, request timeout |
| `NVIDIA_MAX_TOKENS` | `4096`, room for reasoning and final JSON |
| `NVIDIA_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Required service-account JSON, one line |
| `GOOGLE_SHEET_ID` | Required sheet ID; share sheet with the service account as Editor |
| `GOOGLE_SHEET_TAB` | `Logs` |
| `NUTRITION_API_KEY` | Optional; empty disables API Ninjas |
| `CALORIE_MULTIPLIER` | `1.0` (unadjusted), finite and greater than zero |
| `NUTRITION_DB_MIN_RATIO` | `0.6`, between 0 and 1 |
| `ALLOWED_CHAT_ID` | Optional; restrict who can claim and use the bot |
| `TIMEZONE` | `Asia/Kolkata` |
| `REMINDER_MORNING` | `08:30` |
| `REMINDER_AFTERNOON` | `14:00` |
| `REMINDER_NIGHT` | `20:30` |
| `DB_PATH` | `/data/bot_state.db` in Docker, `bot_state.db` otherwise |

Google Sheet column order is unchanged:
`Date, Meal, Food, Category, Calories, Protein_g, LoggedAt`.
Both Date and LoggedAt use the configured timezone.
The default is Nemotron 3 Ultra 550B through NVIDIA's API, with thinking enabled
and a 240-second timeout. Truncated output is rejected rather than partially parsed. The owner prefers careful handling over low latency.
A greeting is handled locally and never sent for estimation or cached as food.
For other uncached messages the model must explicitly confirm `is_food=true` before
any estimate or dietary clarification is accepted. Missing/false classification,
invalid nutrients and provider errors leave the meal unsaved. Network calls run
off the event loop so reminders remain responsive while inference is pending.
Totals for real foods are estimates; describe portions and use `/fix` for known values.

Greetings open a friendly guide and persistent buttons for logging, testing, reports,
editing, correcting totals, skipping and saved usuals. Mixed greetings plus food still
reach the estimator. Test mode is clearly labelled and stays on until Log meal,
Back to logging, /cancel or a new local day. Reports remain available during tests;
real mutations require leaving test mode. Pending real edits survive a test session.

The bot remembers only explicit meal nicknames and corrections. This is saved personal
memory, not model training or automatically learned nutrition facts. Usuals are shown
with their original portion for confirmation before logging. Old automatic estimates
are rechecked under the new portion policy; manual corrections remain usable.

The 19 October 2026 deployment switches new estimates from 1.3 to 1.0. Historical
Sheet rows and saved meal totals remain unchanged. A one-time backed-up migration
preserves previously corrected final calories in the cache when changing the factor.

Multi-user access is not enabled. Removing the owner guard alone would expose the
shared Sheet and mix records. Before inviting users, reports, corrections, caches,
exports and reminders need per-user isolation, admission controls and load testing.

## Restaurant nutrition and reasoning

Explicit chicken, mutton, fish and egg descriptions resolve as non-veg; paneer and
plant-based substitutes are handled appropriately. A biryani order count is not
assumed to mean one cup. The bot asks for the amount eaten before estimating.

The active path is now restaurant-neutral: `nvidia_client.py` identifies any named
restaurant and dish, then `nutrition_research.py` searches via `web_nutrition.py`.
India is the default market unless another country is explicit. DDGS searches Bing
(with a bounded Yahoo fallback), opens public pages and follows nutrition PDF links.
No paid search account or brand allowlist is required. Search engines/sites can
still block requests or lack useful nutrition; successful coverage is not guaranteed.

The larger NVIDIA Ultra model reasons over fetched page/PDF excerpts and checks
country, variant, size/crust, whole-order versus slice/per-100g basis, and all named
combo components. Published totals require source IDs and verbatim row/header/
serving evidence. The application validates the quotes and nutrient numbers against
fetched text, then calculates scaled totals itself. A separate reasoning call checks
nutrient columns, country/variant and serving arithmetic before a published match
is accepted. Unverified third-party publishers cannot establish a published match. Matching is still model-assisted;
this does not independently verify restaurant recipes or guarantee ±10% accuracy.
Published matches include source URLs, serving assumptions and retrieval date.
Insufficient evidence falls back to a clearly labelled recipe estimate, or a focused
question if the food/portion is still unclear. Search failures are stated explicitly.

Automatic guesses are rechecked so an old cache cannot bypass a new restaurant
search. Explicit user corrections remain authoritative; usuals and historical logs
are preserved. Generic API nutrition cannot overwrite restaurant research. The old
`restaurant_nutrition.py` is retained only as a legacy snapshot, not used by the bot.
This is on-demand research and explicit personal memory, not automatic training.

Public-page retrieval blocks private/link-local addresses, pins validated IPs,
revalidates redirects, and bounds page sizes, fetches and PDF extraction. Web text
is untrusted data. PDF extraction runs separately with a deadline and a Linux memory
limit. A public-page cache retains successfully read pages for up to 7 days (100 pages
maximum), separately from meal records. Search still runs afresh. If a live read
fails, a cached page can be used with its original read date and an explicit label;
expired pages cannot establish a match. This cache can also be seeded with public
pages fetched from another authorized environment when a publisher blocks the VM.
Search receives only the extracted restaurant, dish and country, not chat IDs
or the user's full conversation. `/test` uses this same research pipeline without
writing meals, Sheets, aliases or the automatic food cache.

## Azure setup

Use an existing VM with capacity or a small permitted Ubuntu LTS/Debian VM. Check
https://azure.microsoft.com/pricing/calculator/ for the region and size. SSH key
authentication only; inbound TCP 22 restricted to your current public IP. No bot port
is needed. Do not start polling until the old Railway/local instance is stopped.

On the VM:

```bash
sudo apt update
sudo apt install -y docker.io git
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
# Log out and in to apply Docker group membership.
sudo mkdir -p /opt/calorie-bot
sudo chown "$USER:$USER" /opt/calorie-bot
git clone https://github.com/DhanushSS/Cllorie_bot.git /opt/calorie-bot
cd /opt/calorie-bot
# Select the reviewed deployment branch/commit before running.
cp .env.example .env
chmod 600 .env
nano .env
./deploy.sh
```

For a VM with about 1 GB RAM, add 1–2 GB swap if none exists. Never overwrite an
existing swap file. Example for a new `/swapfile-calorie-bot`:

```bash
sudo fallocate -l 2G /swapfile-calorie-bot
sudo chmod 600 /swapfile-calorie-bot
sudo mkswap /swapfile-calorie-bot
sudo swapon /swapfile-calorie-bot
echo '/swapfile-calorie-bot none swap sw 0 0' | sudo tee -a /etc/fstab
```

`deploy.sh` pulls the current branch with fast-forward only, builds before stopping
the running bot, keeps its previous image, and recreates exactly one container with
`--restart unless-stopped` and the named `calorie-bot-data` volume. Startup failures
restore the previous image when one exists. The 12-second startup check is a process
check, not proof of Telegram delivery; verify commands and the next reminder too.

## Operate / rollback

```bash
cd /opt/calorie-bot
./deploy.sh
docker logs --tail 50 calorie-bot
docker restart calorie-bot
docker inspect -f '{{.RestartCount}}' calorie-bot
```

Rollback (after at least one successful update):

```bash
docker stop -t 30 calorie-bot
docker rm calorie-bot
docker run -d --name calorie-bot --restart unless-stopped \
  --env-file /opt/calorie-bot/.env -e DB_PATH=/data/bot_state.db \
  -v calorie-bot-data:/data --log-opt max-size=10m --log-opt max-file=3 \
  calorie-bot:previous
```

Never delete the data volume during redeployment. Stop before moving the SQLite file;
use SQLite's backup API if copying a live local database. Ownership restored from the
volume automatically reschedules all three reminders without another `/start`.

## Tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m py_compile *.py tests/*.py
.venv/bin/python -m pytest -q
```

Tests use temporary SQLite files and mocked external services. Live checks require
separate credentials and explicit observations: phone commands, Sheet writes, restart,
VM reboot and arrival of a scheduled reminder. Test passes alone do not prove those.

## Verification and next steps

See [deployment and verification status](docs/DEPLOYMENT_STATUS.txt) for the deployed
revision, 205 passing checks, live-source test and remaining limits.
The [multi-user assessment](docs/MULTI_USER_ASSESSMENT.txt) describes the isolation
work needed before inviting other people. Private credentials and data stay off GitHub.
