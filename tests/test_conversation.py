import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import bot
import config
import conversation
import db
import nvidia_client
import sheets_client

run = asyncio.run


def callback(update, data):
    update.callback_query = SimpleNamespace(answer=AsyncMock(), data=data, message=update.message)
    update.message.chat = update.effective_chat
    return update


@pytest.mark.parametrize('text',['hey bro','Hiiii!!','hello there','hi, how are you?','how r u','namaste','good evening','gud mrng'])
def test_friendly_greeting_opens_features_and_keyboard(text,update,ctx):
    update.message.text=text
    run(bot.handle_text(update,ctx))
    reply=update.message.reply_text.call_args
    assert '/test' in reply.args[0]
    assert 'Test food' in [b.text for row in reply.kwargs['reply_markup'].keyboard for b in row]
    nvidia_client.estimate.assert_not_called()
    sheets_client.append_entry.assert_not_called()


@pytest.mark.parametrize('text',['hey, i had 2 idlis and sambar','hi i ate one banana','2 chapatis n dal','one medium banana'])
def test_food_with_casual_text_is_not_discarded(text,update,ctx):
    update.message.text=text
    run(bot.handle_text(update,ctx))
    nvidia_client.estimate.assert_called_once()
    assert nvidia_client.estimate.call_args.args[1]==text
    sheets_client.append_entry.assert_called_once()


def test_semantic_greeting_still_opens_menu(monkeypatch,update,ctx):
    update.message.text='sup fam'
    monkeypatch.setattr(nvidia_client,'estimate',Mock(return_value={'is_food':False,'intent':'greeting','needs_clarification':False}))
    run(bot.handle_text(update,ctx))
    assert '/test' in ctx.bot.send_message.call_args.kwargs['text']
    assert ctx.bot.send_message.call_args.kwargs['reply_markup'].is_persistent
    sheets_client.append_entry.assert_not_called()


def test_dry_run_does_not_change_real_food_state(update,ctx):
    day=bot.today_local().isoformat()
    db.set_pending_clarification('42','morning','Veg?','biryani',day,True)
    db.set_kv('edit_target:42',f'{day}:morning')
    ctx.args=['one banana']
    run(bot.cmd_test(update,ctx))
    sheets_client.append_entry.assert_not_called()
    sheets_client.update_entry.assert_not_called()
    assert db.get_cached_food('one banana') is None
    assert db.get_day_status('42',day)=={}
    assert db.get_pending_clarification('42')['food_text']=='biryani'
    assert db.get_kv('edit_target:42')==f'{day}:morning'
    assert bot.test_active('42')
    assert 'nothing saved' in ctx.bot.send_message.call_args.kwargs['text']


def test_dry_run_uses_existing_correction_without_changing_it(update,ctx):
    db.set_cached_food('banana',200,5,'veg','manually corrected')
    before=db.get_cached_food('banana')
    ctx.args=['banana']
    run(bot.cmd_test(update,ctx))
    assert db.get_cached_food('banana')==before
    nvidia_client.estimate.assert_not_called()
    assert '260 kcal' in ctx.bot.send_message.call_args.kwargs['text']
    sheets_client.append_entry.assert_not_called()


def test_test_clarification_buttons_stay_isolated(monkeypatch,update,ctx):
    day=bot.today_local().isoformat()
    db.set_pending_clarification('42','morning','Veg?','curry',day,True)
    estimate=Mock(side_effect=[
        {'is_food':True,'needs_clarification':True,'clarification_kind':'portion','clarification_question':'How much rice?','clarification_options':['1 cup','2 cups']},
        {'is_food':True,'needs_clarification':False,'calories':200,'protein_g':4}])
    monkeypatch.setattr(nvidia_client,'estimate',estimate)
    ctx.args=['rice']
    run(bot.cmd_test(update,ctx))
    pending=bot.test_pending('42')
    run(bot.on_answer(callback(update,f"answer:test:{pending['question_id']}:0"),ctx))
    assert 'Clarification: 1 cup' in estimate.call_args.args[1]
    assert bot.test_pending('42') is None
    assert db.get_pending_clarification('42')['food_text']=='curry'
    assert db.get_cached_food('rice. clarification: 1 cup') is None
    sheets_client.append_entry.assert_not_called()
    sheets_client.update_entry.assert_not_called()


def test_test_mode_accepts_typed_followup(monkeypatch,update,ctx):
    db.set_kv('test_mode:42',bot.today_local().isoformat())
    db.set_kv('test_clar:42',json.dumps({'date':bot.today_local().isoformat(),'food_text':'rice','meal':'morning'}))
    update.message.text='about 200 grams cooked'
    run(bot.handle_text(update,ctx))
    assert 'Clarification: about 200 grams cooked' in nvidia_client.estimate.call_args.args[1]
    sheets_client.append_entry.assert_not_called()


@pytest.mark.parametrize('handler',[bot.cmd_skip,bot.cmd_edit,bot.cmd_fix,bot.cmd_correct,bot.cmd_remember,bot.cmd_forget])
def test_test_mode_blocks_real_mutating_commands(handler,update,ctx):
    day=bot.today_local().isoformat()
    db.set_kv('test_mode:42',day)
    db.upsert_meal_status('42',day,'morning','logged','banana','veg',130,4)
    before=db.get_day_status('42',day)
    ctx.args=['morning','200','5']
    run(handler(update,ctx))
    assert db.get_day_status('42',day)==before
    sheets_client.update_entry.assert_not_called()
    assert 'test mode' in update.message.reply_text.call_args.args[0]


def test_cancel_leaves_history_and_cache(update,ctx):
    day=bot.today_local().isoformat()
    db.set_kv('test_mode:42',day)
    db.set_cached_food('banana',100,4,'veg','AI estimate')
    db.upsert_meal_status('42',day,'morning','logged','banana','veg',130,4)
    run(bot.cmd_cancel(update,ctx))
    assert not bot.test_active('42')
    assert db.get_day_status('42',day)['morning']['calories']==130
    assert db.get_cached_food('banana')['calories']==100


def test_quantity_question_does_not_get_diet_buttons(monkeypatch,ctx):
    monkeypatch.setattr(nvidia_client,'estimate',Mock(return_value={'is_food':True,'needs_clarification':True,'clarification_kind':'portion','clarification_question':'How much veg biryani?','clarification_options':[]}))
    run(bot.finalize_entry('42',bot.today_local().isoformat(),'morning','veg biryani',ctx,False))
    keys=ctx.bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard
    assert [b.text for row in keys for b in row]==['Cancel']
    sheets_client.append_entry.assert_not_called()


def test_stale_clarification_does_not_change_meal(update,ctx):
    day=bot.today_local().isoformat()
    db.set_pending_clarification('42','morning','Amount?','rice',day,False,['1 cup'],'new')
    run(bot.on_answer(callback(update,'answer:log:old:0'),ctx))
    nvidia_client.estimate.assert_not_called()
    sheets_client.append_entry.assert_not_called()


def test_old_cache_is_rechecked_under_new_accuracy_policy(ctx):
    db.set_cached_food('rice',100,2,'veg','old AI guess')
    conn=db._conn();conn.execute('UPDATE food_cache SET policy_version=1');conn.commit();conn.close()
    run(bot.finalize_entry('42',bot.today_local().isoformat(),'morning','rice',ctx,False))
    nvidia_client.estimate.assert_called_once()


def test_unadjusted_estimate_keeps_history_unchanged(monkeypatch,ctx):
    monkeypatch.setattr(config,'CALORIE_MULTIPLIER',1.0)
    db.upsert_meal_status('42','2026-10-18','morning','logged','older meal','veg',130,4)
    run(bot.finalize_entry('42','2026-10-19','morning','banana',ctx,False))
    assert db.get_day_status('42','2026-10-19')['morning']['calories']==100
    assert db.get_day_status('42','2026-10-18')['morning']['calories']==130


def test_remember_and_recall_require_confirmation(update,ctx):
    day=bot.today_local().isoformat()
    run(bot.finalize_entry('42',day,'morning','2 idlis and a bowl of sambar',ctx,False))
    ctx.args=['breakfast']
    run(bot.cmd_remember(update,ctx))
    assert db.list_usuals('42')[0]['food_text']=='2 idlis and a bowl of sambar'
    sheets_client.append_entry.reset_mock()
    update.message.text='my usual breakfast'
    run(bot.handle_text(update,ctx))
    sheets_client.append_entry.assert_not_called()
    assert 'Same amount' in update.message.reply_text.call_args.args[0]
    assert len(update.message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard[0])==2


def test_usual_is_private_and_forgetting_keeps_history(update,ctx):
    db.remember_meal('42','breakfast','2 idlis')
    db.remember_meal('99','breakfast','other meal')
    ctx.args=['breakfast']
    run(bot.cmd_forget(update,ctx))
    assert not db.list_usuals('42')
    assert db.list_usuals('99')[0]['food_text']=='other meal'
    sheets_client.update_entry.assert_not_called()


def test_cross_user_usual_callback_cannot_read_or_log(update,ctx):
    db.remember_meal('99','private','private food')
    item=db.list_usuals('99')[0]
    run(bot.on_navigation(callback(update,f"use_usual:log:{item['id']}"),ctx))
    sheets_client.append_entry.assert_not_called()
    assert 'private food' not in update.message.reply_text.call_args.args[0]


def test_stale_remember_button_does_not_save_wrong_meal(update,ctx):
    db.set_kv('last_logged:42',json.dumps({'food_text':'new meal','memory_id':'new'}))
    run(bot.on_navigation(callback(update,'remember:old'),ctx))
    assert db.get_kv('remember_pending:42') is None


def test_correction_button_supports_natural_labelled_numbers(update,ctx):
    day=bot.today_local().isoformat()
    db.upsert_meal_status('42',day,'morning','logged','banana','veg',130,4)
    run(bot.on_navigation(callback(update,f'correct:{day}:morning'),ctx))
    update.callback_query=None
    update.message.text='actually 20 g protein and 450 kcal'
    run(bot.handle_text(update,ctx))
    entry=db.get_day_status('42',day)['morning']
    assert entry['calories']==450 and entry['protein']==20
    assert db.get_cached_food('banana')['calories']==pytest.approx(450/1.3)
    assert db.get_kv('fix_pending:42') is None


def test_unknown_usual_does_not_invent_food(update,ctx):
    update.message.text='my usual lunch'
    run(bot.handle_text(update,ctx))
    nvidia_client.estimate.assert_not_called()
    sheets_client.append_entry.assert_not_called()


def test_menu_today_routes_to_report(monkeypatch,update,ctx):
    handler=AsyncMock()
    monkeypatch.setattr(bot,'cmd_today',handler)
    update.message.text='Today'
    run(bot.handle_text(update,ctx))
    handler.assert_awaited_once()


def test_test_greeting_keeps_dry_run_label(ctx):
    run(bot.finalize_entry('42',bot.today_local().isoformat(),'morning','hi',ctx,False,test_mode=True))
    assert 'Test only' in ctx.bot.send_message.call_args.kwargs['text']
    nvidia_client.estimate.assert_not_called()


def test_back_to_logging_resumes_correction(update,ctx):
    day=bot.today_local().isoformat()
    db.set_kv('fix_pending:42',json.dumps(dict(date=day,meal='morning')))
    db.set_kv('test_mode:42',day)
    run(bot.route_action('log',update,ctx))
    assert not bot.test_active('42')
    assert 'Back to your correction' in update.message.reply_text.call_args.args[0]


def test_factor_migration_preserves_manual_totals_and_history():
    from migrate_calorie_factor import migrate
    db.set_cached_food('corrected',500/1.3,20,'veg','manually corrected')
    db.set_cached_food('automatic',100,2,'veg','AI estimate')
    db.upsert_meal_status('42','2026-10-18','morning','logged','old','veg',500,20)
    first=migrate(config.DB_PATH)
    assert first['corrected_cache_rows']==1
    assert db.get_cached_food('corrected')['calories']==pytest.approx(500)
    assert db.get_cached_food('automatic')['calories']==100
    assert db.get_day_status('42','2026-10-18')['morning']['calories']==500
    assert migrate(config.DB_PATH)['already_applied']
    assert db.get_cached_food('corrected')['calories']==pytest.approx(500)


def test_followup_preserves_answered_question(monkeypatch,update,ctx):
    day=bot.today_local().isoformat()
    db.set_pending_clarification('42','morning','Veg or non-veg?','biryani',day,False,['Veg','Non-veg'],'diet')
    run(bot.on_answer(callback(update,'answer:log:diet:0'),ctx))
    description=nvidia_client.estimate.call_args.args[1]
    assert description=='biryani. Answered question: Veg or non-veg? Clarification: Veg'
    assert 'ALREADY been answered' in nvidia_client.SYSTEM_PROMPT


def test_greeting_cannot_become_food_through_answered_question(ctx):
    run(bot.finalize_entry('42',bot.today_local().isoformat(),'morning','Hi. Answered question: Veg or non-veg? Clarification: Veg',ctx,False))
    nvidia_client.estimate.assert_not_called()
    sheets_client.append_entry.assert_not_called()
