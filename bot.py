import logging
from datetime import date, timedelta

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    "/help - show this again"
)


def _is_owner(chat_id: str) -> bool:
    owner = db.get_kv("chat_id")
    return owner is None or owner == chat_id


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
    if chat_id:
        schedule_reminders(app, chat_id)


# ---------------- commands ----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    existing = db.get_kv("chat_id")
    if not existing:
        db.set_kv("chat_id", chat_id)
    elif existing != chat_id:
        await update.message.reply_text("This bot is already set up for another chat.")
        return
    schedule_reminders(context.application, chat_id)
    await update.message.reply_text(HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = date.today()
    df = sheets_client.get_entries_df(start_date=d, end_date=d)
    await update.message.reply_text(charts.summary_text(df))


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_report(update, context, days=7, title="Last 7 days")


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_report(update, context, days=30, title="Last 30 days")


async def _send_report(update: Update, context: ContextTypes.DEFAULT_TYPE, days: int, title: str) -> None:
    end = date.today()
    start = end - timedelta(days=days - 1)
    df = sheets_client.get_entries_df(start_date=start, end_date=end)
    await update.message.reply_text(f"{title}\n\n{charts.summary_text(df)}")
    buf = charts.build_chart(df, title)
    if buf:
        await update.message.reply_photo(photo=buf)


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    today = date.today().isoformat()
    meal = db.earliest_pending_meal(chat_id, today)
    if meal is None:
        await update.message.reply_text("Nothing to skip — everything's already logged/skipped today.")
        return
    db.upsert_meal_status(chat_id, today, meal, "skipped")
    db.clear_pending_clarification(chat_id)
    nxt = db.earliest_pending_meal(chat_id, today)
    msg = f"⏭️ Skipped {config.MEAL_LABELS[meal]}."
    if nxt:
        msg += f" You can log {config.MEAL_LABELS[nxt]} right now if you'd like — just tell me what you had."
    await update.message.reply_text(msg)


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    today = date.today().isoformat()
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
    today = date.today().isoformat()
    meal = db.earliest_pending_meal(chat_id, today)
    if meal is None:
        return  # everything already logged/skipped today, stay quiet

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

    if result is None:
        await context.bot.send_chat_action(chat_id=int(chat_id), action="typing")
        result = nvidia_client.estimate(label, food_text)

    if result["needs_clarification"]:
        question = result["clarification_question"] or "Could you clarify — veg or non-veg?"
        db.set_pending_clarification(chat_id, meal, question, food_text)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Veg", callback_data="clarveg"),
              InlineKeyboardButton("Non-veg", callback_data="clarnonveg")]]
        )
        await context.bot.send_message(chat_id=int(chat_id), text=question, reply_markup=kb)
        return

        # Once the food is unambiguous, resolve the actual numbers in this priority order:
    # 1. Cache - guarantees the exact same description always gives the exact same
    #    numbers, whether it was previously priced by the database or the AI.
    # 2. A real nutrition database (API Ninjas) - deterministic, covers common/generic
    #    foods. Not configured or no match -> falls through to the AI's own estimate.
    # 3. The AI's estimate from earlier in this function, as a last resort.
    cache_key = db.food_cache_key(food_text)
    cached = db.get_cached_food(cache_key)
    if cached:
        calories = cached["calories"]
        protein = cached["protein_g"]
        category = cached["category"]
        source_note = cached["notes"]
    else:
        db_result = nutrition_api.lookup(food_text)
        if db_result:
            calories, protein = db_result
            category = result.get("category", "unknown")
            source_note = "from nutrition database"
        else:
            calories = result.get("calories") or 0
            protein = result.get("protein_g") or 0
            category = result.get("category", "unknown")
            ai_note = result.get("notes")
            source_note = f"AI estimate{': ' + ai_note if ai_note else ''}"
        db.set_cached_food(cache_key, calories, protein, category, source_note)

    if is_edit:
        sheets_client.update_entry(today, meal, food_text, category, calories, protein)
    else:
        sheets_client.append_entry(today, meal, food_text, category, calories, protein)

    db.upsert_meal_status(chat_id, today, meal, "logged", food_text, category, calories, protein)
    db.clear_pending_clarification(chat_id)

    verb = "Updated" if is_edit else "Logged"
    await context.bot.send_message(
        chat_id=int(chat_id),
        text=f"✅ {verb} {label}: {food_text}\n~{calories:.0f} kcal · {protein:.0f} g protein\n({source_note})",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if not _is_owner(chat_id):
        return

    text = update.message.text.strip()
    today = date.today().isoformat()

    edit_meal = db.get_kv(f"edit_target:{chat_id}")
    if edit_meal:
        db.delete_kv(f"edit_target:{chat_id}")
        await finalize_entry(chat_id, today, edit_meal, text, context, is_edit=True)
        return

    pending = db.get_pending_clarification(chat_id)
    if pending:
        combined = f"{pending['food_text']}. Clarification: {text}"
        await finalize_entry(chat_id, today, pending["meal"], combined, context, is_edit=False)
        return

    meal = db.earliest_pending_meal(chat_id, today)
    if meal is None:
        await update.message.reply_text(
            "All your meals are already logged for today ✅ Use /edit to fix something, or /today to view your log."
        )
        return

    await finalize_entry(chat_id, today, meal, text, context, is_edit=False)


# ---------------- callback buttons ----------------

async def on_clarification_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat.id)
    pending = db.get_pending_clarification(chat_id)
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
    today = date.today().isoformat()
    try:
        await finalize_entry(chat_id, today, pending["meal"], combined, context, is_edit=False)
    except Exception:
        logger.exception("Error finalizing entry from clarification button")
        await context.bot.send_message(
            chat_id=int(chat_id),
            text="Something went wrong logging that — could you just type what you had instead?",
        )


async def on_skip_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat.id)
    try:
        meal = query.data.split("_", 1)[1]
        today = date.today().isoformat()
        db.upsert_meal_status(chat_id, today, meal, "skipped")
        db.clear_pending_clarification(chat_id)
        await context.bot.send_message(chat_id=int(chat_id), text=f"⏭️ Skipped {config.MEAL_LABELS[meal]}.")
    except Exception:
        logger.exception("Error handling skip button")
        await context.bot.send_message(chat_id=int(chat_id), text="Couldn't process that — try /skip instead.")

async def on_edit_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat.id)
    meal = query.data.split("_", 1)[1]
    db.set_kv(f"edit_target:{chat_id}", meal)
    await context.bot.send_message(
        chat_id=int(chat_id), text=f"Okay — what did you actually have for {config.MEAL_LABELS[meal]}?"
    )

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global fallback: any exception a handler doesn't catch itself lands here.
    We log it AND tell the user something broke, instead of them getting silence."""
    logger.exception("Unhandled exception while processing update: %s", update)
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

    app.add_handler(CallbackQueryHandler(on_clarification_button, pattern="^clar(veg|nonveg)$"))
    app.add_handler(CallbackQueryHandler(on_skip_button, pattern="^skip_"))
    app.add_handler(CallbackQueryHandler(on_edit_button, pattern="^edit_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    logger.info("Bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
