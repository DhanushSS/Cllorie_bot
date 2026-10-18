import logging
import asyncio
import sys
import re
from functools import wraps
from datetime import datetime, date, timedelta

from openai import APIConnectionError, APIStatusError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import charts
import config
import db
import nutrition_api
import nvidia_client
import sheets_client

class SecretFormatter(logging.Formatter):
    def format(self, record):
        text = super().format(record)
        for secret in (config.TELEGRAM_BOT_TOKEN, config.NVIDIA_API_KEY,
                       config.NUTRITION_API_KEY, config.GOOGLE_SERVICE_ACCOUNT_JSON):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", text)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(SecretFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("calorie-bot")

HELP_TEXT = (
    "Hey! 👋 I'm your calorie & protein tracker.\n\n"
    "I'll check in with you 3 times a day — morning, afternoon and night — and ask what you ate. "
    "Just describe it in plain words (e.g. '2 idlis and sambar' or 'chicken biryani, 1 plate'). "
    "If something's ambiguous (like biryani), I'll ask a quick follow-up before logging it.\n\n"
    "I won't move on to the next meal until the current one is logged or skipped.\n\n"
    "Commands:\n"
    "/today - today's log\n"
    "/week - last 7 days report + chart\n"
    "/month - last 30 days report + chart\n"
    "/edit - fix a meal you already logged today\n"
    "/skip - skip whichever meal is currently pending\n"
    "/fix <morning|afternoon|night> <kcal> <protein> - correct final totals\n"
    "/help - show this again"
)


def today_local() -> date:
    return datetime.now(config.TIMEZONE).date()


def _is_owner(chat_id: str) -> bool:
    if config.ALLOWED_CHAT_ID and config.ALLOWED_CHAT_ID != chat_id:
        return False
    return db.get_kv("chat_id") == chat_id


def owner_only(func):
    @wraps(func)
    async def guarded(update, context):
        if not update.effective_chat or not _is_owner(str(update.effective_chat.id)):
            if update.callback_query:
                await update.callback_query.answer()
            return
        return await func(update, context)
    return guarded


def is_conversation(text: str) -> bool:
    # Also reject historic false entries such as "Hi. It is veg." before cache access.
    original = re.split(r"\.\s*(?:Clarification:|It is (?:veg|non-veg)\.)", text, maxsplit=1, flags=re.I)[0]
    normalized = " ".join(re.sub(r"[^\w\s]", " ", original.casefold()).split())
    return normalized in {
        "hi", "hii", "hiii", "hello", "hey", "hey bot", "hello bot", "hi bot",
        "good morning", "good afternoon", "good evening", "good night",
        "how are you", "how are you doing", "thanks", "thank you", "ok", "okay",
        "bye", "test", "start", "help", "yo", "namaste", "vanakkam",
    }


def current_pending(chat_id, today):
    pending = db.get_pending_clarification(chat_id)
    if pending and pending.get("date") != today:
        db.clear_pending_clarification(chat_id)
        return None
    return pending


def schedule_reminders(app: Application, chat_id: str) -> None:
    jq = app.job_queue
    slots = [
        ("morning", config.REMINDER_MORNING),
        ("afternoon", config.REMINDER_AFTERNOON),
        ("night", config.REMINDER_NIGHT),
    ]
    for meal, t in slots:
        name = f"reminder_{meal}_{chat_id}"
        for j in jq.get_jobs_by_name(name):
            j.schedule_removal()
        jq.run_daily(reminder_callback, time=t, chat_id=int(chat_id), name=name, data={"meal": meal})
    logger.info("Scheduled reminders for chat %s", chat_id)


async def post_init(app: Application) -> None:
    chat_id = db.get_kv("chat_id")
    if chat_id and _is_owner(chat_id):
        schedule_reminders(app, chat_id)


# ---------------- commands ----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if config.ALLOWED_CHAT_ID and chat_id != config.ALLOWED_CHAT_ID:
        return
    existing = db.get_kv("chat_id")
    if not existing:
        db.set_kv("chat_id", chat_id)
    elif existing != chat_id:
        await update.message.reply_text("This bot is already set up for another chat.")
        return
    schedule_reminders(context.application, chat_id)
    await update.message.reply_text(HELP_TEXT)


@owner_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


@owner_only
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = today_local()
    df = await asyncio.to_thread(sheets_client.get_entries_df, start_date=d, end_date=d)
    await update.message.reply_text(charts.summary_text(df))


@owner_only
async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_report(update, context, days=7, title="Last 7 days")


@owner_only
async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_report(update, context, days=30, title="Last 30 days")


async def _send_report(update: Update, context: ContextTypes.DEFAULT_TYPE, days: int, title: str) -> None:
    end = today_local()
    start = end - timedelta(days=days - 1)
    df = await asyncio.to_thread(sheets_client.get_entries_df, start_date=start, end_date=end)
    await update.message.reply_text(f"{title}\n\n{charts.summary_text(df)}")
    buf = charts.build_chart(df, title)
    if buf:
        await update.message.reply_photo(photo=buf)


@owner_only
async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    today = today_local().isoformat()
    meal = db.earliest_pending_meal(chat_id, today)
    if meal is None:
        await update.message.reply_text("Nothing to skip — everything's already logged/skipped today.")
        return
    db.upsert_meal_status(chat_id, today, meal, "skipped")
    db.clear_pending_clarification(chat_id)
    db.delete_kv(f"edit_target:{chat_id}")
    nxt = db.earliest_pending_meal(chat_id, today)
    msg = f"⏭️ Skipped {config.MEAL_LABELS[meal]}."
    if nxt:
        msg += f" You can log {config.MEAL_LABELS[nxt]} right now if you'd like — just tell me what you had."
    await update.message.reply_text(msg)


@owner_only
async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    today = today_local().isoformat()
    status = db.get_day_status(chat_id, today)
    logged = [m for m in config.MEALS if status.get(m, {}).get("status") == "logged"]
    if not logged:
        await update.message.reply_text("No logged meals today yet to edit.")
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(config.MEAL_LABELS[m], callback_data=f"edit_{m}")] for m in logged]
    )
    await update.message.reply_text("Which meal do you want to correct?", reply_markup=kb)


# ---------------- reminders ----------------

async def reminder_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(context.job.chat_id)
    today = today_local().isoformat()
    meal = db.earliest_pending_meal(chat_id, today)
    if meal is None:
        return  # everything already logged/skipped today, stay quiet

    if not _is_owner(chat_id):
        return
    triggered_meal = context.job.data["meal"]
    label = config.MEAL_LABELS[meal]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Skip {label}", callback_data=f"skip_{meal}")]])

    if meal != triggered_meal:
        text = (
            f"⏰ It's {config.MEAL_LABELS[triggered_meal]} time, but you haven't logged {label} yet.\n"
            f"What did you have for {label}? (or tap Skip)"
        )
    else:
        text = f"🍽️ {label} time! What did you have?"

    await context.bot.send_message(chat_id=int(chat_id), text=text, reply_markup=kb)


# ---------------- food logging / estimation ----------------

async def finalize_entry(
    chat_id: str,
    today: str,
    meal: str,
    food_text: str,
    context: ContextTypes.DEFAULT_TYPE,
    is_edit: bool,
    result: dict | None = None,
) -> None:
    label = config.MEAL_LABELS[meal]
    if is_conversation(food_text):
        db.clear_pending_clarification(chat_id)
        await context.bot.send_message(chat_id=int(chat_id), text="Please tell me what you ate, including the quantity. A greeting is not a meal, so nothing was logged.")
        return

    cache_key = db.food_cache_key(food_text)
    cached = db.get_cached_food(cache_key)
    if cached:
        calories = cached["calories"]
        protein = cached["protein_g"]
        category = cached["category"]
        source_note = cached["notes"]
    else:
        if result is None:
            await context.bot.send_chat_action(chat_id=int(chat_id), action="typing")
            await context.bot.send_message(chat_id=int(chat_id), text="Checking your meal and portion…")
            try:
                result = await asyncio.to_thread(nvidia_client.estimate, label, food_text)
            except (APIConnectionError, APIStatusError) as exc:
                logger.warning("NVIDIA estimate unavailable (%s)", type(exc).__name__)
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text="The nutrition AI is unavailable or took too long. Nothing was saved. Please try again shortly.",
                )
                return

        if result.get("is_food") is not True:
            await context.bot.send_message(chat_id=int(chat_id), text="I couldn't identify a food in that message. Please tell me what you ate and how much; nothing was logged.")
            return

        if result["needs_clarification"]:
            question = result.get("clarification_question") or "Could you clarify — veg or non-veg?"
            db.set_pending_clarification(chat_id, meal, question, food_text, today, is_edit)
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Veg", callback_data="clarveg"),
                  InlineKeyboardButton("Non-veg", callback_data="clarnonveg")]]
            )
            await context.bot.send_message(chat_id=int(chat_id), text=question, reply_markup=kb)
            return

        db_result = await asyncio.to_thread(nutrition_api.lookup, food_text)
        ai_cal = nutrition_api.nutrient(result.get("calories"))
        ai_pro = nutrition_api.nutrient(result.get("protein_g"))
        valid_ai = ai_cal is not None and ai_cal > 0 and ai_pro is not None
        category = result.get("category") or "unknown"
        if db_result and not (valid_ai and db_result[0] < config.NUTRITION_DB_MIN_RATIO * ai_cal):
            calories, protein = db_result
            source_note = "from nutrition database"
        elif valid_ai:
            calories, protein = ai_cal, ai_pro
            source_note = "AI estimate: portion-adjusted" if db_result else "AI estimate"
            if result.get("notes"):
                source_note += ": " + str(result["notes"])
        else:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text="I couldn't get a usable estimate. Please retype the food and portion; nothing was saved.",
            )
            return
        db.set_cached_food(cache_key, calories, protein, category, source_note)

    # Cache/database/AI all store the raw number - the multiplier is applied once,
    # right here, regardless of which of those it came from.
    calories = round((calories or 0) * config.CALORIE_MULTIPLIER, 1)

    if is_edit:
        await asyncio.to_thread(sheets_client.update_entry, today, meal, food_text, category, calories, protein)
    else:
        await asyncio.to_thread(sheets_client.append_entry, today, meal, food_text, category, calories, protein)

    db.upsert_meal_status(chat_id, today, meal, "logged", food_text, category, calories, protein)
    db.clear_pending_clarification(chat_id)
    db.delete_kv(f"edit_target:{chat_id}")

    verb = "Updated" if is_edit else "Logged"
    await context.bot.send_message(
        chat_id=int(chat_id),
        text=f"✅ {verb} {label}: {food_text}\n~{calories:.0f} kcal · {protein:.0f} g protein\n({source_note})",
    )


@owner_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if not _is_owner(chat_id):
        return

    text = update.message.text.strip()
    if is_conversation(text):
        await update.message.reply_text("Hi! Tell me what you ate and how much, for example ‘2 idlis and sambar’. Use /today to see your log or /help for commands.")
        return
    today = today_local().isoformat()

    pending = current_pending(chat_id, today)
    if pending:
        combined = f"{pending['food_text']}. Clarification: {text}"
        await finalize_entry(chat_id, today, pending["meal"], combined, context,
                             is_edit=pending.get("is_edit", False))
        return

    edit_target = db.get_kv(f"edit_target:{chat_id}")
    edit_meal = None
    if edit_target:
        target_date, _, target_meal = edit_target.partition(":")
        if target_date == today and target_meal in config.MEALS:
            edit_meal = target_meal
        else:
            db.delete_kv(f"edit_target:{chat_id}")
    if edit_meal:
        await finalize_entry(chat_id, today, edit_meal, text, context, is_edit=True)
        return

    meal = db.earliest_pending_meal(chat_id, today)
    if meal is None:
        await update.message.reply_text(
            "All your meals are already logged for today ✅ Use /edit to fix something, or /today to view your log."
        )
        return

    await finalize_entry(chat_id, today, meal, text, context, is_edit=False)


# ---------------- callback buttons ----------------

@owner_only
async def on_clarification_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat.id)
    pending = current_pending(chat_id, today_local().isoformat())
    if not pending:
        # The bot restarted (e.g. a redeploy) since this question was asked, so it no
        # longer remembers what it was waiting for. Tell the user instead of doing nothing.
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="Sorry, I lost track of that after a restart — just type what you had "
                 "(e.g. 'chicken biryani, 1 plate') and I'll log it fresh.",
        )
        return
    answer = "veg" if query.data == "clarveg" else "non-veg"
    combined = f"{pending['food_text']}. It is {answer}."
    today = today_local().isoformat()
    try:
        await finalize_entry(chat_id, today, pending["meal"], combined, context, is_edit=pending.get("is_edit", False))
    except Exception:
        logger.exception("Error finalizing entry from clarification button")
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="Something went wrong logging that — could you just type what you had instead?",
        )


@owner_only
async def on_skip_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat.id)
    try:
        meal = query.data.split("_", 1)[1]
        today = today_local().isoformat()
        if meal != db.earliest_pending_meal(chat_id, today):
            await context.bot.send_message(chat_id=int(chat_id), text="That reminder is no longer current. Use /skip.")
            return
        db.upsert_meal_status(chat_id, today, meal, "skipped")
        db.clear_pending_clarification(chat_id)
        db.delete_kv(f"edit_target:{chat_id}")
        nxt = db.earliest_pending_meal(chat_id, today)
        text = f"⏭️ Skipped {config.MEAL_LABELS[meal]}."
        if nxt:
            text += f" You can log {config.MEAL_LABELS[nxt]} now."
        await context.bot.send_message(chat_id=int(chat_id), text=text)
    except Exception:
        logger.exception("Error handling skip button")
        await context.bot.send_message(chat_id=int(chat_id), text="Couldn't process that — try /skip instead.")

@owner_only
async def on_edit_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat.id)
    try:
        meal = query.data.split("_", 1)[1]
        today = today_local().isoformat()
        if db.get_day_status(chat_id, today).get(meal, {}).get("status") != "logged":
            await context.bot.send_message(chat_id=int(chat_id), text="No logged meal to edit. Use /edit for today's choices.")
            return
        db.clear_pending_clarification(chat_id)
        db.set_kv(f"edit_target:{chat_id}", f"{today}:{meal}")
        await context.bot.send_message(
            chat_id=int(chat_id), text=f"Okay — what did you actually have for {config.MEAL_LABELS[meal]}?"
        )
    except Exception:
        logger.exception("Error handling edit button")
        await context.bot.send_message(chat_id=int(chat_id), text="Couldn't process that — try /edit instead.")


@owner_only
async def cmd_fix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) != 3 or args[0].lower() not in config.MEALS:
        await update.message.reply_text("Usage: /fix morning|afternoon|night <final kcal> <protein grams>")
        return
    meal = args[0].lower()
    calories, protein = (nutrition_api.nutrient(value) for value in args[1:])
    if calories is None or protein is None:
        await update.message.reply_text("Calories and protein must be finite, non-negative numbers.")
        return
    chat_id = str(update.effective_chat.id)
    today = today_local().isoformat()
    entry = db.get_day_status(chat_id, today).get(meal, {})
    if entry.get("status") != "logged":
        await update.message.reply_text("Log that meal first, then use /fix to correct it.")
        return
    calories, protein = round(calories, 1), round(protein, 1)
    food, category = entry["food_text"], entry["category"]
    await asyncio.to_thread(sheets_client.update_entry, today, meal, food, category, calories, protein)
    db.upsert_meal_status(chat_id, today, meal, "logged", food, category, calories, protein)
    db.set_cached_food(db.food_cache_key(food), calories / config.CALORIE_MULTIPLIER,
                       protein, category, "manually corrected")
    db.clear_pending_clarification(chat_id)
    db.delete_kv(f"edit_target:{chat_id}")
    await update.message.reply_text(f"✅ Corrected {config.MEAL_LABELS[meal]}: {calories:g} kcal · {protein:g} g protein")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global fallback: any exception a handler doesn't catch itself lands here.
    We log it AND tell the user something broke, instead of them getting silence."""
    logger.error("Unhandled exception while processing update", exc_info=context.error)
    chat_id = None
    if isinstance(update, Update) and update.effective_chat:
        chat_id = update.effective_chat.id
    if chat_id is not None:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Something went wrong processing that — I've logged it. Please try again.",
            )
        except Exception:
            pass

def main() -> None:
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("fix", cmd_fix))

    app.add_handler(CallbackQueryHandler(on_clarification_button, pattern="^clar(veg|nonveg)$"))
    app.add_handler(CallbackQueryHandler(on_skip_button, pattern="^skip_"))
    app.add_handler(CallbackQueryHandler(on_edit_button, pattern="^edit_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    logger.info("Bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
