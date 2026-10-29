import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import bot
import config
import db
import sheets_client


def friend_update(chat_id):
    chat = SimpleNamespace(id=chat_id, type="private")
    return SimpleNamespace(effective_chat=chat,
                           effective_user=SimpleNamespace(id=chat_id),
                           message=SimpleNamespace(text="one banana", reply_text=AsyncMock(),
                                                   reply_photo=AsyncMock()),
                           callback_query=None)


def friend_context():
    queue = SimpleNamespace(get_jobs_by_name=Mock(return_value=[]), run_daily=Mock())
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(),
                                               send_chat_action=AsyncMock()),
                           application=SimpleNamespace(job_queue=queue), args=[])


def test_friends_get_separate_logs_reports_and_corrections(monkeypatch):
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    monkeypatch.setattr(sheets_client, "get_entries_df", Mock())
    a, b = friend_update(99), friend_update(100)
    ca, cb = friend_context(), friend_context()
    asyncio.run(bot.cmd_start(a, ca))
    asyncio.run(bot.cmd_start(b, cb))
    assert db.is_registered("99") and db.is_registered("100")
    assert db.get_kv("chat_id") == "42"
    assert not db.reminders_enabled("99")

    asyncio.run(bot.handle_text(a, ca))
    day = bot.today_local().isoformat()
    assert db.get_day_status("99", day)["morning"]["status"] == "logged"
    assert db.get_day_status("100", day) == {}
    assert db.get_day_status("42", day) == {}
    sheets_client.append_entry.assert_not_called()

    asyncio.run(bot.cmd_today(a, ca))
    asyncio.run(bot.cmd_today(b, cb))
    assert "130 kcal" in a.message.reply_text.call_args.args[0]
    assert "No entries" in b.message.reply_text.call_args.args[0]
    sheets_client.get_entries_df.assert_not_called()

    ca.args = ["morning", "220", "9"]
    asyncio.run(bot.cmd_fix(a, ca))
    assert db.get_cached_food("one banana", chat_id="99")["calories"] == 220 / 1.3
    assert db.get_cached_food("one banana", chat_id="100") is None
    assert db.get_cached_food("one banana", chat_id="42") is None
    sheets_client.update_entry.assert_not_called()

    asyncio.run(bot.handle_text(b, cb))
    assert db.get_day_status("100", day)["morning"]["calories"] == 130
    assert db.get_day_status("99", day)["morning"]["calories"] == 220


def test_unregistered_and_group_messages_are_rejected(monkeypatch):
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    update, ctx = friend_update(99), friend_context()
    asyncio.run(bot.handle_text(update, ctx))
    assert not db.is_registered("99")
    assert ctx.bot.send_message.await_count == 0
    update.effective_chat.type = "group"
    asyncio.run(bot.cmd_start(update, ctx))
    assert not db.is_registered("99")
    update.effective_chat.type = "private"
    update.effective_user.id = 100
    asyncio.run(bot.cmd_start(update, ctx))
    assert not db.is_registered("99")


def test_reminder_opt_in_is_per_user(monkeypatch):
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    update, ctx = friend_update(99), friend_context()
    asyncio.run(bot.cmd_start(update, ctx))
    ctx.application.job_queue.run_daily.assert_not_called()
    ctx.args = ["on"]
    asyncio.run(bot.cmd_reminders(update, ctx))
    assert db.reminders_enabled("99")
    assert not db.reminders_enabled("42")
    assert ctx.application.job_queue.run_daily.call_count == 3
    ctx.args = ["off"]
    asyncio.run(bot.cmd_reminders(update, ctx))
    assert not db.reminders_enabled("99")
