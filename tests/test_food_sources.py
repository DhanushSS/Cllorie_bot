import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import bot, config, db, food_rules, restaurant_nutrition as menu, nvidia_client, sheets_client, nutrition_api

run=asyncio.run

@pytest.mark.parametrize('text,expected',[
    ('one chicken biryani','non-veg'),('mutton biriyani','non-veg'),('fish curry','non-veg'),
    ('egg biryani','non-veg'),('chkn biryani','non-veg'),('paneer biryani','veg'),
    ('vegan chicken biryani','veg'),('veg biryani without chicken','veg'),
    ('biryani. Answered question: Chicken or veg? Clarification: Veg','veg')])
def test_explicit_diet_facts(text,expected):
    assert food_rules.category(text)==expected

@pytest.mark.parametrize('food',['one chicken biryani','1 chicken biryani','chkn biryani'])
def test_named_meat_goes_to_portion_not_diet(food,ctx):
    run(bot.finalize_entry('42',bot.today_local().isoformat(),'morning',food,ctx,False))
    pending=db.get_pending_clarification('42')
    assert 'how much' in pending['question'].lower()
    assert 'Veg' not in pending['options'] and 'Non-veg' not in pending['options']
    nvidia_client.estimate.assert_not_called()
    sheets_client.append_entry.assert_not_called()

@pytest.mark.parametrize('text', ['one chicken biryani, full restaurant order','300 g chicken biryani','half restaurant order of veg biryani'])
def test_clear_biryani_amount_reaches_estimator(text,ctx):
    run(bot.finalize_entry('42',bot.today_local().isoformat(),'morning',text,ctx,False))
    nvidia_client.estimate.assert_called_once()
    sheets_client.append_entry.assert_called_once()

@pytest.mark.parametrize('text,key',[('Meghana','meghana'),('Meghna','meghana'),('Nandhana','nandhana'),('Nandana','nandhana')])
def test_brand_spelling_aliases(text,key):
    assert menu.brand(text)==key


def test_meghana_unspecified_variant_is_not_invented():
    r=menu.resolve('one meghana biryani',date(2026,10,19))
    assert r['needs_clarification'] and r['clarification_kind']=='food'
    assert r['calories'] is None


def test_nandhana_chicken_still_needs_specific_variant():
    r=menu.resolve('nandhana chicken biryani',date(2026,10,19))
    assert r['clarification_options']==['Hyderabadi chicken','Nellore chicken','Supreme boneless','Other variant']

@pytest.mark.parametrize('text,cal,protein',[
    ('meghana chicken biryani full listed order',1615.3,61.8),
    ('meghana boneless chicken biryani half listed order',723.2,32.1),
    ('meghana special boneless biryani full order',1456.2,64.7),
    ('nandhana hyderabadi chicken biryani full order',1290,52.2),
    ('nandhana supreme boneless biryani half order',727.5,38.1),
    ('nandhana nellore chicken biryani quarter order',253.1,4.3)])
def test_published_values_scaled_from_matched_menu_item(text,cal,protein):
    r=menu.resolve(text,date(2026,10,19))
    assert not r['needs_clarification']
    assert r['calories']==cal and r['protein_g']==protein
    assert 'https://www.swiggy.com/' in r['source_note']
    assert '2026-10-19' in r['source_note']

@pytest.mark.parametrize('text',[
    'nandhana chicken 555 biryani full order',
    'nandhana high protein supreme boneless biryani full order',
    'meghana chicken biryani full order without sides',
    'meghana chicken biryani full order and coke',
    'meghana chicken biryani 300 g',
    'nandhana bucket supreme biryani full order'])
def test_nonmatching_product_or_serving_cannot_borrow_menu_totals(text):
    assert menu.resolve(text,date(2026,10,19)) is None


def test_menu_expires_instead_of_claiming_current_values():
    assert menu.resolve('meghana chicken biryani full order',date(2027,3,1)) is None


def test_veg_choice_does_not_accidentally_become_paneer():
    text='Meghana biryani. Answered question: Which? Clarification: Veg / paneer'
    assert menu.resolve(text,date(2026,10,19))['clarification_options']==['Paneer biryani','Special veg biryani']
    text+='. Answered question: Which? Clarification: Special veg biryani. Answered question: Amount? Clarification: Full listed order'
    r=menu.resolve(text,date(2026,10,19))
    assert r['calories']==1327.3


def test_published_test_result_bypasses_ai_database_and_writes(ctx,monkeypatch):
    monkeypatch.setattr(config,'CALORIE_MULTIPLIER',1.0)
    food='Meghana boneless chicken biryani half listed order'
    run(bot.finalize_entry('42',bot.today_local().isoformat(),'morning',food,ctx,False,test_mode=True))
    assert '723.2 kcal' in ctx.bot.send_message.call_args.kwargs['text']
    nvidia_client.estimate.assert_not_called()
    nutrition_api.lookup.assert_not_called()
    sheets_client.append_entry.assert_not_called()
    assert db.get_cached_food(db.food_cache_key(food)) is None


def test_old_automatic_guess_is_invalidated_but_history_kept(ctx):
    db.set_cached_food('300 g chicken biryani',450,15,'veg','AI estimate')
    conn=db._conn();conn.execute('UPDATE food_cache SET policy_version=2');conn.commit();conn.close()
    run(bot.finalize_entry('42',bot.today_local().isoformat(),'morning','300 g chicken biryani',ctx,False))
    nvidia_client.estimate.assert_called_once()
    assert db.get_day_status('42',bot.today_local().isoformat())['morning']['category']=='non-veg'


def test_manual_correction_remains_authoritative(ctx):
    db.set_cached_food('meghana chicken biryani full order',1500,60,'non-veg','manually corrected')
    run(bot.finalize_entry('42',bot.today_local().isoformat(),'morning','meghana chicken biryani full order',ctx,False,test_mode=True))
    assert 'manually corrected' in ctx.bot.send_message.call_args.kwargs['text']
    nvidia_client.estimate.assert_not_called()


def test_thinking_request_has_budget_and_discards_truncated_result(monkeypatch):
    import importlib
    module=importlib.reload(nvidia_client)
    create=Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason='length',message=SimpleNamespace(content='{"calories":450}'))]))
    monkeypatch.setattr(module.client.chat.completions,'create',create)
    monkeypatch.setattr(config,'NVIDIA_MODEL','nvidia/nemotron-3-ultra-550b-a55b')
    r=module.estimate('Lunch','full restaurant order of chicken biryani')
    assert create.call_args.kwargs['extra_body']=={'chat_template_kwargs':{'enable_thinking':True}}
    assert create.call_args.kwargs['max_tokens']>=4096
    assert r['is_food'] is False and r['calories'] is None

@pytest.mark.parametrize('text',[
    'meghana mushroom biryani full order', 'nandhana veg biryani full order',
    'nandhana hyderabadi chicken biryani full order without sweet',
    'meghana chicken biryani full order with extra chicken'])
def test_other_variants_and_recipe_changes_are_not_catalog_matches(text):
    assert menu.resolve(text,date(2026,10,19)) is None


def test_recipe_estimate_keeps_uncertainty_and_brand_warning(monkeypatch,ctx):
    monkeypatch.setattr(nvidia_client,'estimate',Mock(return_value=dict(is_food=True,needs_clarification=False,calories=800,protein_g=30,category='non-veg',calorie_range=[600,1000],protein_range=[20,40],notes='Assumes a half restaurant order.')))
    run(bot.finalize_entry('42',bot.today_local().isoformat(),'morning','Nandhana chicken 555 biryani half restaurant order',ctx,False,test_mode=True))
    reply=ctx.bot.send_message.call_args.kwargs['text']
    assert '600–1000' in reply and '20–40' in reply
    assert 'not verified brand nutrition' in reply
    nutrition_api.lookup.assert_not_called()


def test_model_cannot_reask_diet_for_named_chicken(monkeypatch,ctx):
    monkeypatch.setattr(nvidia_client,'estimate',Mock(return_value=dict(is_food=True,needs_clarification=True,clarification_kind='diet',clarification_question='Veg or non-veg?',clarification_options=['Veg','Non-veg'])))
    run(bot.finalize_entry('42',bot.today_local().isoformat(),'morning','300 g chicken biryani',ctx,False))
    reply=ctx.bot.send_message.call_args.kwargs['text']
    assert 'Veg or non-veg' not in reply
    sheets_client.append_entry.assert_not_called()


def test_unrecognized_addition_is_not_silently_dropped():
    assert menu.resolve('meghana chicken biryani full order coke',date(2026,10,19)) is None
