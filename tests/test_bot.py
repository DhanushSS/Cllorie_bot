import asyncio
import logging
from datetime import datetime, timezone, date
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest
import requests
import bot
import config
import db
import nvidia_client
import nutrition_api
import sheets_client

run = asyncio.run


@pytest.mark.parametrize('text', [
    '{"notes":"ok"}', '```json\n{"notes":"ok"}\n```',
    '<think>stray { unclosed } {</think>{"notes":"ok"}',
    '{"notes":"braces { } and escaped \\"quote\\""}',
])
def test_extract_json(text):
    assert isinstance(nvidia_client._extract_json(text)['notes'], str)


@pytest.mark.parametrize('items,expected', [
    ([{'calories': 100, 'protein_g': 2}], (100, 2)),
    ([{'calories': '100.5', 'protein_g': '2'}], (100.5, 2)),
    ([{'calories': 'premium only', 'protein_g': 'premium only'}], None),
    ([{'calories': 100, 'protein_g': 'premium only'}], None),
    ([{'calories': 100, 'protein_g': 2}, {'calories': 'locked', 'protein_g': 3}], None),
    ([{'calories': 'nan', 'protein_g': 1}], None),
    ([], None), ({'error': 'no access'}, None), ([None], None),
])
def test_nutrition(items, expected, monkeypatch):
    # Fixture replaces public lookup; reload the function without changing mocks elsewhere.
    import importlib
    api = importlib.reload(nutrition_api)
    monkeypatch.setattr(api.requests, 'get', Mock(return_value=SimpleNamespace(
        raise_for_status=Mock(), json=lambda: items)))
    assert api.lookup('1 banana') == expected


def test_nutrition_network_error(monkeypatch):
    import importlib
    api = importlib.reload(nutrition_api)
    monkeypatch.setattr(api.requests, 'get', Mock(side_effect=requests.Timeout()))
    assert api.lookup('1 banana') is None


@pytest.mark.parametrize('database,expected,note', [
    ((59.9, 2), 130, 'portion-adjusted'), ((60, 2), 78, 'database'),
    ((90, 2), 117, 'database'), (None, 130, 'AI estimate'),
])
def test_priority_and_threshold(database, expected, note, monkeypatch, ctx):
    monkeypatch.setattr(nutrition_api, 'lookup', Mock(return_value=database))
    run(bot.finalize_entry('42', '2026-10-18', 'morning', 'banana', ctx, False))
    assert db.get_day_status('42', '2026-10-18')['morning']['calories'] == expected
    cached = db.get_cached_food('banana')
    assert cached['calories'] == pytest.approx(expected / 1.3)
    assert note in cached['notes']
    monkeypatch.setattr(nvidia_client, 'estimate', Mock(side_effect=AssertionError('cache must bypass AI')))
    monkeypatch.setattr(nutrition_api, 'lookup', Mock(side_effect=AssertionError('cache must bypass DB')))
    run(bot.finalize_entry('42', '2026-10-18', 'afternoon', 'banana', ctx, False))
    assert db.get_day_status('42', '2026-10-18')['afternoon']['calories'] == expected


def test_fix_cache_not_double_multiplied(ctx, update):
    day = bot.today_local().isoformat()
    db.upsert_meal_status('42', day, 'morning', 'logged', 'banana', 'veg', 100, 4)
    ctx.args = ['morning', '260', '10']
    run(bot.cmd_fix(update, ctx))
    assert db.get_cached_food('banana')['calories'] == 200
    assert db.get_day_status('42', day)['morning']['calories'] == 260
    run(bot.finalize_entry('42', day, 'afternoon', 'banana', ctx, False))
    assert db.get_day_status('42', day)['afternoon']['calories'] == 260
    assert db.get_day_status('42', day)['afternoon']['protein'] == 10


@pytest.mark.parametrize('args', [['morning','nan','5'], ['morning','-1','5'], ['night','20'], ['snack','20','3']])
def test_invalid_fix(args, ctx, update):
    ctx.args = args
    run(bot.cmd_fix(update, ctx))
    sheets_client.update_entry.assert_not_called()


def test_ist_day_boundary(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 17, 19, 0, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(bot, 'datetime', Frozen)
    assert bot.today_local() == date(2026, 10, 18)


def test_gating_skip(ctx, update):
    day = bot.today_local().isoformat()
    ctx.job = SimpleNamespace(chat_id=42, data={'meal': 'afternoon'})
    run(bot.reminder_callback(ctx))
    assert 'Breakfast' in ctx.bot.send_message.call_args.kwargs['text']
    assert db.earliest_pending_meal('42', day) == 'morning'
    run(bot.cmd_skip(update, ctx))
    assert db.earliest_pending_meal('42', day) == 'afternoon'
    run(bot.handle_text(update, ctx))
    assert db.get_day_status('42', day)['afternoon']['status'] == 'logged'


def test_stale_skip_cannot_overwrite_logged(ctx, update):
    day = bot.today_local().isoformat()
    db.upsert_meal_status('42', day, 'morning', 'logged', 'banana', 'veg', 130, 4)
    update.callback_query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(chat=SimpleNamespace(id=42)), data='skip_morning')
    run(bot.on_skip_button(update, ctx))
    assert db.get_day_status('42', day)['morning']['status'] == 'logged'


@pytest.mark.parametrize('callback', [False, True])
def test_edit_clarification_updates_row(callback, ctx, update):
    day = bot.today_local().isoformat()
    db.set_pending_clarification('42', 'morning', 'Veg?', '1 cup biryani', day, True)
    update.message.text = 'veg'
    update.callback_query = SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(chat=SimpleNamespace(id=42)), data='clarveg') if callback else None
    run(bot.on_clarification_button(update, ctx) if callback else bot.handle_text(update, ctx))
    sheets_client.update_entry.assert_called_once()
    sheets_client.append_entry.assert_not_called()
    assert db.get_pending_clarification('42') is None


def test_old_pending_cleared():
    db.set_pending_clarification('42','night','Veg?','biryani','2026-10-17',True)
    assert bot.current_pending('42','2026-10-18') is None


@pytest.mark.parametrize('command', [bot.cmd_today,bot.cmd_skip,bot.cmd_edit,bot.cmd_fix,bot.cmd_week,bot.cmd_month,bot.handle_text])
def test_nonowner_cannot_read_or_mutate(command, update, ctx):
    update.effective_chat.id = 99
    run(command(update, ctx))
    update.message.reply_text.assert_not_called()
    ctx.bot.send_message.assert_not_called()
    assert not db.get_day_status('99', bot.today_local().isoformat())


def test_allowed_chat_blocks_claim(update, ctx, monkeypatch):
    db.delete_kv('chat_id')
    monkeypatch.setattr(config,'ALLOWED_CHAT_ID','99')
    run(bot.cmd_start(update,ctx))
    assert db.get_kv('chat_id') is None


def test_restart_schedules_persisted_owner():
    job_queue = SimpleNamespace(get_jobs_by_name=Mock(return_value=[]),run_daily=Mock())
    run(bot.post_init(SimpleNamespace(job_queue=job_queue)))
    assert job_queue.run_daily.call_count == 3
    assert [c.kwargs['time'].tzinfo for c in job_queue.run_daily.call_args_list] == [config.TIMEZONE]*3


def test_invalid_estimate_never_saved(monkeypatch, ctx):
    monkeypatch.setattr(nvidia_client,'estimate',Mock(return_value={'is_food':True,'needs_clarification':False,'calories':None,'protein_g':None}))
    run(bot.finalize_entry('42','2026-10-18','morning','invalid',ctx,False))
    sheets_client.append_entry.assert_not_called()
    assert db.get_cached_food('invalid') is None
    assert db.earliest_pending_meal('42','2026-10-18') == 'morning'


def test_logs_redact_token_even_in_exception():
    formatter = bot.SecretFormatter('%(message)s')
    record = logging.LogRecord('httpx',logging.ERROR,'test',1,'request https://api.telegram.org/bot123456:secret_token/getMe',(),None)
    assert 'bot123456:' not in formatter.format(record)


def test_sheet_timestamp_and_column_order(monkeypatch):
    # Exercise the real write functions (autouse fixture mocks them).
    import importlib
    module = importlib.reload(sheets_client)
    ws=SimpleNamespace(append_row=Mock(),get_all_values=Mock(return_value=[module.HEADER,['2026-10-18','morning']]),update=Mock())
    monkeypatch.setattr(module,'_sheet',lambda:ws)
    module.append_entry('2026-10-18','morning','banana','veg',130,4)
    row=ws.append_row.call_args.args[0]
    assert row[:6] == ['2026-10-18','morning','banana','veg',130,4]
    assert row[6].endswith('+05:30')
    module.update_entry('2026-10-18','morning','banana','veg',260,10)
    assert ws.update.call_args.kwargs['range_name']=='A2:G2'
    assert ws.update.call_args.kwargs['values'][0][4:6]==[260,10]


@pytest.mark.parametrize('days,command', [(7,bot.cmd_week),(30,bot.cmd_month)])
def test_report_sends_real_chart(days,command,monkeypatch,ctx,update):
    import pandas as pd
    frame=pd.DataFrame([{'Date':bot.today_local(),'Calories':130,'Protein_g':4}])
    monkeypatch.setattr(sheets_client,'get_entries_df',Mock(return_value=frame))
    run(command(update,ctx))
    sheets_client.get_entries_df.assert_called_once()
    kwargs=sheets_client.get_entries_df.call_args.kwargs
    assert (kwargs['end_date']-kwargs['start_date']).days == days-1
    photo=update.message.reply_photo.call_args.kwargs['photo']
    assert photo.read(8)==b'\x89PNG\r\n\x1a\n'
    assert '130 kcal' in update.message.reply_text.call_args.args[0]


def test_clarification_does_not_log_early(monkeypatch,ctx):
    monkeypatch.setattr(nvidia_client,'estimate',Mock(return_value={
        'is_food':True,'needs_clarification':True,'clarification_question':'Veg or non-veg?'}))
    run(bot.finalize_entry('42','2026-10-18','morning','biryani',ctx,False))
    sheets_client.append_entry.assert_not_called()
    assert db.get_pending_clarification('42')['date']=='2026-10-18'
    assert db.earliest_pending_meal('42','2026-10-18')=='morning'
    buttons=ctx.bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard
    assert [b.text for row in buttons for b in row]==['Veg','Non-veg','Cancel']


def test_provider_timeout_keeps_meal_pending(monkeypatch,ctx):
    from openai import APITimeoutError
    import httpx
    monkeypatch.setattr(nvidia_client,'estimate',Mock(side_effect=APITimeoutError(request=httpx.Request('POST','https://example.test'))))
    run(bot.finalize_entry('42','2026-10-18','morning','banana',ctx,False))
    sheets_client.append_entry.assert_not_called()
    assert db.earliest_pending_meal('42','2026-10-18')=='morning'
    assert 'Nothing was saved' in ctx.bot.send_message.call_args.kwargs['text']


def test_estimation_does_not_block_event_loop(monkeypatch,ctx):
    import threading
    entered,release=threading.Event(),threading.Event()
    def estimate(*args):
        entered.set()
        release.wait(2)
        return {'is_food':True,'needs_clarification':False,'calories':100,'protein_g':4}
    monkeypatch.setattr(nvidia_client,'estimate',estimate)
    async def check():
        task=asyncio.create_task(bot.finalize_entry('42','2026-10-18','morning','banana',ctx,False))
        try:
            for _ in range(100):
                if entered.is_set(): break
                await asyncio.sleep(.001)
            assert entered.is_set()
            assert not task.done()
            assert not release.is_set()
        finally:
            release.set()
            await task
    run(check())


def test_estimator_uses_fast_model_settings(monkeypatch):
    import importlib
    module=importlib.reload(nvidia_client)
    create=Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"needs_clarification":false,"calories":105,"protein_g":1.3}'))]))
    monkeypatch.setattr(module.client.chat.completions,'create',create)
    monkeypatch.setattr(config,'NVIDIA_MODEL','openai/gpt-oss-20b')
    result=module.estimate('Breakfast','one banana')
    assert result['calories']==105
    assert create.call_args.kwargs['extra_body']=={'reasoning_effort':'low'}


@pytest.mark.parametrize('text',['hi','Hi!','hello','thanks','good morning','how are you','Hi. It is veg.'])
def test_greeting_never_estimates_or_logs(text,update,ctx):
    update.message.text=text
    run(bot.handle_text(update,ctx))
    nvidia_client.estimate.assert_not_called()
    sheets_client.append_entry.assert_not_called()
    assert db.earliest_pending_meal('42',bot.today_local().isoformat())=='morning'
    reply=update.message.reply_text.call_args.args[0]
    assert ('Anytime' in reply) if text=='thanks' else ('Tell me what you ate' in reply)


def test_old_greeting_cache_is_never_used(ctx):
    db.set_cached_food('hi. it is veg.',550,15,'veg','old false entry')
    run(bot.finalize_entry('42','2026-10-18','morning','Hi. It is veg.',ctx,False))
    sheets_client.append_entry.assert_not_called()
    assert db.earliest_pending_meal('42','2026-10-18')=='morning'


@pytest.mark.parametrize('result',[
    {'is_food':False,'needs_clarification':False,'calories':550,'protein_g':15},
    {'needs_clarification':False,'calories':550,'protein_g':15},
    {'is_food':False,'needs_clarification':True,'clarification_question':'Veg?'}
])
def test_nonfood_or_missing_classification_never_saved(result,monkeypatch,ctx):
    monkeypatch.setattr(nvidia_client,'estimate',Mock(return_value=result))
    run(bot.finalize_entry('42','2026-10-18','morning','unrelated message',ctx,False))
    sheets_client.append_entry.assert_not_called()
    assert db.get_cached_food('unrelated message') is None
    assert db.get_pending_clarification('42') is None


def test_greeting_preserves_real_pending_clarification(update,ctx):
    day=bot.today_local().isoformat()
    db.set_pending_clarification('42','morning','Veg?','biryani',day,False)
    update.message.text='hello'
    run(bot.handle_text(update,ctx))
    assert db.get_pending_clarification('42')['food_text']=='biryani'
