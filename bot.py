import logging
import asyncio
import sys
import json
import secrets
from types import SimpleNamespace
import re
from functools import wraps
from datetime import datetime, date, timedelta

from openai import APIConnectionError, APIStatusError

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
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
import conversation
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

HELP_TEXT = conversation.HELP_TEXT


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


def is_conversation(text):
    return conversation.is_conversation(text)


def test_active(chat_id):
    return db.get_kv(f"test_mode:{chat_id}") == today_local().isoformat()


def test_pending(chat_id):
    raw = db.get_kv(f"test_clar:{chat_id}")
    if not raw:
        return None
    pending = json.loads(raw)
    if pending.get("date") != today_local().isoformat():
        db.delete_kv(f"test_clar:{chat_id}")
        return None
    return pending


def clear_test(chat_id):
    db.delete_kv(f"test_mode:{chat_id}")
    db.delete_kv(f"test_clar:{chat_id}")


async def welcome(update, context):
    mode = "Test mode is on — nothing you try here will be logged. Tap Log meal when you're done.\n\n" if test_active(str(update.effective_chat.id)) else ""
    await update.message.reply_text(mode + HELP_TEXT, reply_markup=conversation.menu_keyboard())


async def require_live(update):
    if test_active(str(update.effective_chat.id)):
        await update.message.reply_text("You're in test mode. Tap Log meal first if you want to change your real log.", reply_markup=conversation.menu_keyboard())
        return False
    return True


def clarified_food(pending, answer):
    # Preserve the question as well as the answer so the model knows what is settled.
    question = pending.get("question") or "Please clarify the food or portion"
    return f"{pending['food_text']}. Answered question: {question} Clarification: {answer}"


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
    if hasattr(app, "bot"):
        commands = [("start", "Meet your food diary"), ("help", "What the buttons do"),
                    ("test", "Try an estimate without logging"), ("today", "Today's totals"),
                    ("week", "Weekly report and chart"), ("month", "Monthly report and chart"),
                    ("edit", "Replace a logged meal"), ("skip", "Skip the pending meal"),
                    ("fix", "Correct calories and protein"), ("usuals", "Your saved usual meals"),
                    ("remember", "Name your last logged meal"), ("forget", "Remove a saved usual"),
                    ("cancel", "Cancel a follow-up or test mode")]
        try:
            await app.bot.set_my_commands([BotCommand(name, description) for name, description in commands])
        except Exception:
            logger.warning("Could not refresh command menu; existing commands still work")
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
    await welcome(update, context)


@owner_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await welcome(update, context)


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
    if not await require_live(update):
        return
    chat_id = str(update.effective_chat.id)
    today = today_local().isoformat()
    meal = db.earliest_pending_meal(chat_id, today)
    if meal is None:
        await update.message.reply_text("Nothing to skip — everything's already logged/skipped today.")
        return
    db.upsert_meal_status(chat_id, today, meal, "skipped")
    db.clear_pending_clarification(chat_id)
    db.delete_kv(f"fix_pending:{chat_id}")
    db.delete_kv(f"edit_target:{chat_id}")
    nxt = db.earliest_pending_meal(chat_id, today)
    msg = f"⏭️ Skipped {config.MEAL_LABELS[meal]}."
    if nxt:
        msg += f" You can log {config.MEAL_LABELS[nxt]} right now if you'd like — just tell me what you had."
    await update.message.reply_text(msg)


@owner_only
async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_live(update):
        return
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
    test_mode: bool = False,
) -> None:
    label = config.MEAL_LABELS[meal]
    if is_conversation(food_text):
        await context.bot.send_message(chat_id=int(chat_id), text=("Test only — nothing saved.\n\n" if test_mode else "") + HELP_TEXT,
                                       reply_markup=conversation.menu_keyboard())
        return

    cache_key = db.food_cache_key(food_text)
    cached = db.get_cached_food(cache_key)
    # Re-check older automatic guesses under the new portion-validation policy.
    if cached and cached.get("notes") != "manually corrected" and (
            cached.get("policy_version", 1) < 2 or conversation.requires_portion(food_text)):
        cached = None
    if cached:
        calories = cached["calories"]
        protein = cached["protein_g"]
        category = cached["category"]
        source_note = cached["notes"]
    else:
        if result is None:
            await context.bot.send_chat_action(chat_id=int(chat_id), action="typing")
            await context.bot.send_message(chat_id=int(chat_id), text=("Test only — checking that food and portion…" if test_mode else "Got it — checking the food and portion."))
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
            text = HELP_TEXT if result.get("intent") in ("greeting", "help") else "I'm here for food and your meal log. Tell me what you had and roughly how much, or pick a button below."
            await context.bot.send_message(chat_id=int(chat_id), text=("Test only — nothing saved.\n\n" if test_mode else "") + text,
                                           reply_markup=conversation.menu_keyboard())
            return

        if conversation.requires_portion(food_text) and (
                not result["needs_clarification"] or conversation.diet_is_stated(food_text)):
            # A model response cannot supply a portion the user never gave us.
            result = dict(result, needs_clarification=True, clarification_kind="portion",
                          clarification_question="About how much did you have? Tell me the amount, like 1 cup, 2 pieces or 200 g.",
                          clarification_options=[], calories=None, protein_g=None)

        if result["needs_clarification"]:
            question = result.get("clarification_question") or "What food and amount did you have?"
            options = result.get("clarification_options")
            options = [option.strip() for option in options if isinstance(option, str) and 0 < len(option.strip()) <= 40][:4] if isinstance(options, list) else []
            if not options and (result.get("clarification_kind") == "diet" or (not result.get("clarification_kind") and "veg" in question.casefold())):
                options = ["Veg", "Non-veg"]
            question_id = secrets.token_hex(4)
            if test_mode:
                db.set_kv(f"test_clar:{chat_id}", json.dumps(dict(meal=meal, question=question,
                    food_text=food_text, date=today, is_edit=False, options=options, question_id=question_id)))
            else:
                db.set_pending_clarification(chat_id, meal, question, food_text, today, is_edit, options, question_id)
            kb = conversation.clarification_keyboard(options, "test" if test_mode else "log", question_id)
            await context.bot.send_message(chat_id=int(chat_id), text=("Test only — " if test_mode else "") + question, reply_markup=kb)
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
        if not test_mode:
            db.set_cached_food(cache_key, calories, protein, category, source_note)

    # Cache/database/AI all store the raw number - the multiplier is applied once,
    # right here, regardless of which of those it came from.
    calories = round((calories or 0) * config.CALORIE_MULTIPLIER, 1)

    if test_mode:
        db.delete_kv(f"test_clar:{chat_id}")
        await context.bot.send_message(chat_id=int(chat_id),
            text=f"Test only — nothing saved.\n{food_text}\nAbout {calories:g} kcal · {protein:g} g protein\n{source_note}\n\nTry another food, or tap Back to logging.",
            reply_markup=conversation.result_keyboard(True))
        return

    if is_edit:
        await asyncio.to_thread(sheets_client.update_entry, today, meal, food_text, category, calories, protein)
    else:
        await asyncio.to_thread(sheets_client.append_entry, today, meal, food_text, category, calories, protein)

    db.upsert_meal_status(chat_id, today, meal, "logged", food_text, category, calories, protein)
    db.clear_pending_clarification(chat_id)
    db.delete_kv(f"fix_pending:{chat_id}")
    db.delete_kv(f"edit_target:{chat_id}")

    memory_id = secrets.token_hex(4)
    db.set_kv(f"last_logged:{chat_id}", json.dumps(dict(date=today, meal=meal, food_text=food_text, memory_id=memory_id)))
    verb = "Updated" if is_edit else "Saved"
    await context.bot.send_message(
        chat_id=int(chat_id),
        text=f"{verb} {label}: {food_text}\nAbout {calories:g} kcal · {protein:g} g protein\n{source_note}",
        reply_markup=conversation.result_keyboard(memory_id=memory_id),
    )


@owner_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if not _is_owner(chat_id):
        return

    text = update.message.text.strip()
    route = conversation.intent(text)
    if route is None and is_conversation(text):
        route = "greeting"
    if route and await route_action(route, update, context):
        return
    today = today_local().isoformat()

    if db.get_kv(f"remember_pending:{chat_id}"):
        await save_usual(update, context, text)
        return

    alias_name = conversation.normalize(text)
    alias_name = re.sub(r"^(?:my usual|usual|my) ", "", alias_name)
    alias = next((item for item in db.list_usuals(chat_id) if item["name"] == alias_name), None)
    if alias:
        await show_usual(update, alias)
        return
    if conversation.normalize(text).startswith(('my usual ', 'usual ')):
        await update.message.reply_text("I don't have that usual saved yet. Try /usuals, or tell me the actual food and amount.")
        return

    if test_active(chat_id):
        pending = test_pending(chat_id)
        combined = clarified_food(pending, text) if pending else text
        await finalize_entry(chat_id, today, "morning", combined, context, False, test_mode=True)
        return

    correction = db.get_kv(f"fix_pending:{chat_id}")
    if correction:
        await handle_correction(update, context, json.loads(correction))
        return

    pending = current_pending(chat_id, today)
    if pending:
        combined = clarified_food(pending, text)
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
    if test_active(chat_id) or not pending:
        # The bot restarted (e.g. a redeploy) since this question was asked, so it no
        # longer remembers what it was waiting for. Tell the user instead of doing nothing.
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="Sorry, I lost track of that after a restart — just type what you had "
                 "(e.g. 'chicken biryani, 1 plate') and I'll log it fresh.",
        )
        return
    if pending.get("options") and pending["options"] != ["Veg", "Non-veg"]:
        await context.bot.send_message(chat_id=int(chat_id), text="Use the buttons under my latest question, or type your answer.")
        return
    answer = "veg" if query.data == "clarveg" else "non-veg"
    combined = clarified_food(pending, answer)
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
    if not await require_live(SimpleNamespace(effective_chat=update.effective_chat, message=update.callback_query.message)):
        await update.callback_query.answer()
        return
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
        db.delete_kv(f"fix_pending:{chat_id}")
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
    if not await require_live(SimpleNamespace(effective_chat=update.effective_chat, message=update.callback_query.message)):
        await update.callback_query.answer()
        return
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
        db.delete_kv(f"fix_pending:{chat_id}")
        db.set_kv(f"edit_target:{chat_id}", f"{today}:{meal}")
        await context.bot.send_message(
            chat_id=int(chat_id), text=f"Okay — what did you actually have for {config.MEAL_LABELS[meal]}?"
        )
    except Exception:
        logger.exception("Error handling edit button")
        await context.bot.send_message(chat_id=int(chat_id), text="Couldn't process that — try /edit instead.")


@owner_only
async def cmd_fix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_live(update):
        return
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
    db.delete_kv(f"fix_pending:{chat_id}")
    db.delete_kv(f"edit_target:{chat_id}")
    await update.message.reply_text(f"Corrected {config.MEAL_LABELS[meal]}: {calories:g} kcal · {protein:g} g protein")


@owner_only
async def cmd_correct(update, context):
    if not await require_live(update):
        return
    chat_id = str(update.effective_chat.id)
    day = today_local().isoformat()
    status = db.get_day_status(chat_id, day)
    rows = [[InlineKeyboardButton(config.MEAL_LABELS[meal], callback_data=f'correct:{day}:{meal}')]
            for meal in config.MEALS if status.get(meal, {}).get('status') == 'logged']
    if not rows:
        await update.message.reply_text("No meals logged today yet. Once you log one, you can correct its totals here.")
        return
    await update.message.reply_text("Which meal's totals should I correct? This also updates the estimate I remember for that exact food and amount.", reply_markup=InlineKeyboardMarkup(rows))


async def handle_correction(update, context, pending):
    chat_id = str(update.effective_chat.id)
    if pending['date'] != today_local().isoformat():
        db.delete_kv(f"fix_pending:{chat_id}")
        await update.message.reply_text("That correction was for another day. Tap Correct totals to choose a meal from today.")
        return
    text = update.message.text.casefold()
    number = r'(-?\d+(?:\.\d+)?)'
    calories = re.search(number + r'\s*(?:kcal|calories|cals)', text)
    protein = re.search(number + r'\s*(?:g|grams)?\s*(?:of\s+)?protein', text)
    if calories and protein:
        values = [calories.group(1), protein.group(1)]
    elif re.fullmatch(r'\s*' + number + r'[\s,]+' + number + r'\s*', text):
        values = re.findall(number, text)
    else:
        await update.message.reply_text("Send calories and protein grams, like ‘450 20’ or ‘450 kcal and 20 g protein’. Send /cancel to leave it.")
        return
    context.args = [pending['meal'], *values]
    await cmd_fix(update, context)


@owner_only
async def cmd_test(update, context):
    chat_id = str(update.effective_chat.id)
    db.set_kv(f"test_mode:{chat_id}", today_local().isoformat())
    db.delete_kv(f"test_clar:{chat_id}")
    db.delete_kv(f"remember_pending:{chat_id}")
    args = getattr(context, "args", None) or []
    if args:
        await finalize_entry(chat_id, today_local().isoformat(), "morning", " ".join(args), context, False, test_mode=True)
    else:
        await update.message.reply_text(
            "Let's try it. Send a food and amount, like ‘one medium banana’.\n"
            "I'll show calories, protein and the portion I used. If something's unclear, I'll ask.\n\n"
            "This is test mode: no meal, Sheet row, saved estimate or usual gets changed. "
            "Tap Back to logging or send /cancel when you're done.", reply_markup=conversation.test_keyboard())


@owner_only
async def cmd_cancel(update, context):
    chat_id = str(update.effective_chat.id)
    clear_test(chat_id)
    db.clear_pending_clarification(chat_id)
    db.delete_kv(f"fix_pending:{chat_id}")
    db.delete_kv(f"edit_target:{chat_id}")
    db.delete_kv(f"remember_pending:{chat_id}")
    await update.message.reply_text("All good — cancelled. Your saved meals are unchanged. What would you like to do?", reply_markup=conversation.menu_keyboard())


@owner_only
async def cmd_usuals(update, context):
    items = db.list_usuals(str(update.effective_chat.id))
    if not items:
        await update.message.reply_text("No usuals saved yet. After logging a meal, tap Remember this meal and give it a name, like ‘breakfast’. Next time you can say ‘my usual breakfast’.\n\nI also reuse corrections you make with /fix. I don't learn new nutrition facts just by guessing.", reply_markup=conversation.menu_keyboard())
        return
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(item['name'], callback_data=f"usual:{item['id']}")] for item in items])
    await update.message.reply_text("Here's what you've asked me to remember. Tap one to check it before logging. Remove one with /forget followed by its name.", reply_markup=keyboard)


async def show_usual(update, item):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('Log this meal', callback_data=f"use_usual:log:{item['id']}"),
         InlineKeyboardButton('Test it first', callback_data=f"use_usual:test:{item['id']}")],
        [InlineKeyboardButton('Cancel', callback_data='nav_cancel')],
    ])
    await update.message.reply_text(f"Your usual {item['name']}:\n{item['food_text']}\n\nSame amount this time? If it changed, just type the new meal and quantity instead.", reply_markup=keyboard)


@owner_only
async def cmd_remember(update, context):
    if not await require_live(update):
        return
    chat_id = str(update.effective_chat.id)
    raw = db.get_kv(f"last_logged:{chat_id}")
    if not raw:
        await update.message.reply_text("Log a meal first, then I can save it as one of your usuals.")
        return
    last = json.loads(raw)
    db.delete_kv(f"fix_pending:{chat_id}")
    db.set_kv(f"remember_pending:{chat_id}", last['food_text'])
    name = " ".join(getattr(context, 'args', None) or [])
    if name:
        await save_usual(update, context, name)
    else:
        await update.message.reply_text(f"What should I call this meal? Something like ‘breakfast’ or ‘office lunch’ works.\n\n{last['food_text']}\n\nSend /cancel to leave it.")


async def save_usual(update, context, name):
    chat_id = str(update.effective_chat.id)
    food = db.get_kv(f"remember_pending:{chat_id}")
    if not food or test_active(chat_id):
        await update.message.reply_text("Log a meal first, then tap Remember this meal.")
        return
    name = re.sub(r"^(?:my usual|usual|my) ", "", conversation.normalize(name))
    items = db.list_usuals(chat_id)
    if not 1 <= len(name) <= 40 or conversation.intent(name):
        await update.message.reply_text("Pick a short meal nickname, like ‘breakfast’ or ‘office lunch’.")
        return
    if len(items) >= 20 and name not in {item['name'] for item in items}:
        await update.message.reply_text("You've got 20 usuals saved. Remove one with /forget name before adding another.")
        return
    db.remember_meal(chat_id, name, food)
    db.delete_kv(f"remember_pending:{chat_id}")
    await update.message.reply_text(f"Got it — ‘{name}’ means:\n{food}\n\nNext time, say ‘my usual {name}’. I'll show it before logging. You can remove it with /forget {name}.", reply_markup=conversation.menu_keyboard())


@owner_only
async def cmd_forget(update, context):
    if not await require_live(update):
        return
    name = conversation.normalize(" ".join(getattr(context, 'args', None) or []))
    if not name:
        await update.message.reply_text("Send /forget followed by a saved name, like /forget breakfast.")
        return
    removed = db.forget_meal(str(update.effective_chat.id), name)
    await update.message.reply_text(f"Forgot ‘{name}’. Your meal history is unchanged." if removed else "I don't have a usual saved with that name. Try /usuals.")


async def route_action(route, update, context):
    actions = dict(help=cmd_help, today=cmd_today, week=cmd_week, month=cmd_month,
                   test=cmd_test, edit=cmd_edit, fix=cmd_correct, skip=cmd_skip, usuals=cmd_usuals,
                   remember=cmd_remember, cancel=cmd_cancel)
    if route in actions:
        context.args = []
        await actions[route](update, context)
    elif route == 'greeting':
        await welcome(update, context)
    elif route == 'log':
        chat_id = str(update.effective_chat.id)
        clear_test(chat_id)
        db.delete_kv(f"remember_pending:{chat_id}")
        day = today_local().isoformat()
        correction = json.loads(db.get_kv(f'fix_pending:{chat_id}', '{}'))
        edit = db.get_kv(f'edit_target:{chat_id}', '')
        pending = current_pending(chat_id, day)
        if correction.get('date') == day:
            await update.message.reply_text("Back to your correction. Send calories and protein grams, like ‘450 20’, or /cancel.")
        elif pending:
            await update.message.reply_text(pending['question'])
        elif edit.startswith(day + ':') and edit.split(':')[1] in config.MEALS:
            await update.message.reply_text(f"Back to editing {config.MEAL_LABELS[edit.split(':')[1]]}. What food and amount should replace it?")
        else:
            meal = db.earliest_pending_meal(chat_id, today_local().isoformat())
            text = f"What did you have for {config.MEAL_LABELS[meal]}? Include roughly how much." if meal else "Today's meals are covered. Use Edit meal if you want to change one."
            await update.message.reply_text(text, reply_markup=conversation.menu_keyboard())
    elif route in ('thanks', 'ack', 'bye'):
        replies = dict(thanks="Anytime. I'm here when you're ready for the next meal.",
                       ack="Got you. Send your food and amount whenever you're ready.",
                       bye="Catch you later. Your log will be here.")
        await update.message.reply_text(replies[route], reply_markup=conversation.menu_keyboard())
    else:
        return False
    return True


@owner_only
async def on_answer(update, context):
    query = update.callback_query
    await query.answer()
    chat_id = str(update.effective_chat.id)
    try:
        _, scope, question_id, index = query.data.split(':')
        if scope not in ('log', 'test'):
            return
        testing = scope == 'test'
        pending = test_pending(chat_id) if testing else current_pending(chat_id, today_local().isoformat())
        if not pending or pending.get('question_id') != question_id or testing != test_active(chat_id):
            await query.message.reply_text("That question is no longer active. Use the latest buttons, or type a fresh meal.")
            return
        index = int(index)
        if not 0 <= index < len(pending.get('options', [])):
            return
        answer = pending['options'][index]
        combined = clarified_food(pending, answer)
        await finalize_entry(chat_id, today_local().isoformat(), pending['meal'], combined,
                             context, pending.get('is_edit', False), test_mode=testing)
    except Exception:
        logger.exception("Could not handle clarification choice")
        await query.message.reply_text("That didn't go through. Could you type the answer instead?")


@owner_only
async def on_navigation(update, context):
    query = update.callback_query
    await query.answer()
    proxy = SimpleNamespace(effective_chat=update.effective_chat, message=query.message, callback_query=None)
    chat_id = str(update.effective_chat.id)
    context.args = []
    try:
        if query.data.startswith('nav_'):
            await route_action(query.data[4:], proxy, context)
        elif query.data.startswith('correct:'):
            if not await require_live(proxy):
                return
            _, day, meal = query.data.split(':')
            if day != today_local().isoformat() or db.get_day_status(chat_id, day).get(meal, {}).get('status') != 'logged':
                await query.message.reply_text("That meal isn't available for correction. Tap Correct totals for today's choices.")
                return
            db.delete_kv(f"remember_pending:{chat_id}")
            db.clear_pending_clarification(chat_id)
            db.delete_kv(f"edit_target:{chat_id}")
            db.set_kv(f"fix_pending:{chat_id}", json.dumps(dict(date=day, meal=meal)))
            await query.message.reply_text("What are the correct totals? Send calories first, then protein grams — for example ‘450 20’. I'll remember your correction. Send /cancel to leave it.")
        elif query.data.startswith('remember:'):
            last = json.loads(db.get_kv(f"last_logged:{chat_id}", '{}'))
            if last.get('memory_id') != query.data.split(':',1)[1]:
                await query.message.reply_text("That's an older meal. Use /remember for the last meal you logged, or /usuals for saved ones.")
                return
            await cmd_remember(proxy, context)
        elif query.data.startswith('test_sample_'):
            sample = query.data[len('test_sample_'):]
            if sample not in ('banana', 'biryani'):
                return
            context.args = ['one medium banana' if sample == 'banana' else 'biryani']
            await cmd_test(proxy, context)
        else:
            parts = query.data.split(':')
            item = next((x for x in db.list_usuals(chat_id) if x['id'] == int(parts[-1])), None)
            if not item:
                await query.message.reply_text("That usual isn't saved here anymore. Try /usuals.")
                return
            if parts[0] == 'usual':
                await show_usual(proxy, item)
            elif parts[:2] == ['use_usual','test']:
                context.args = [item['food_text']]
                await cmd_test(proxy, context)
            elif parts[:2] == ['use_usual','log']:
                clear_test(chat_id)
                today = today_local().isoformat()
                edit = db.get_kv(f"edit_target:{chat_id}", '')
                edit_date, _, edit_meal = edit.partition(':')
                is_edit = edit_date == today and edit_meal in config.MEALS
                meal = edit_meal if is_edit else db.earliest_pending_meal(chat_id, today)
                if meal:
                    await finalize_entry(chat_id, today, meal, item['food_text'], context, is_edit)
                else:
                    await query.message.reply_text("Today's meals are covered. Use Edit meal to replace one.")
    except Exception:
        logger.exception("Could not handle menu choice")
        await query.message.reply_text("That button didn't go through. Try /help to open the menu again.")


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
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("usuals", cmd_usuals))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CallbackQueryHandler(on_navigation, pattern="^(nav_|correct:|remember:|test_sample_|usual:|use_usual:)"))
    app.add_handler(CallbackQueryHandler(on_answer, pattern="^answer:"))

    app.add_handler(CallbackQueryHandler(on_clarification_button, pattern="^clar(veg|nonveg)$"))
    app.add_handler(CallbackQueryHandler(on_skip_button, pattern="^skip_"))
    app.add_handler(CallbackQueryHandler(on_edit_button, pattern="^edit_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    logger.info("Bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
