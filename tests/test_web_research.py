import asyncio
import copy
from unittest.mock import Mock
import pytest
import bot, config, db, nvidia_client, nutrition_research as nr, web_nutrition as web


DOC = dict(source_class='primary_candidate',id='S1',url='https://restaurant.example.in/nutrition.pdf',retrieved_at='2026-10-19',
           text='India menu. Calories kcal Protein g. Personal pan pizza: 4 slices. Margherita 600 20')
RESULT = dict(is_food=True,needs_clarification=False,calories=9999,protein_g=999,
              basis='published_match',all_components_matched=True,market_and_variant_match=True,
              evidence=[dict(source_id='S1',item='Margherita',calories=600,protein_g=20,
                row_quote='Margherita 600 20',header_quote='Calories kcal Protein g',
                serving_quote='Personal pan pizza: 4 slices',portion_factor=0.5,
                portion_explanation='Ate 2 of the 4 slices in one personal pan pizza.')])


def test_source_arithmetic_overrides_model_total():
    totals=nr.published_totals(RESULT,[DOC])
    assert totals['calories']==300 and totals['protein_g']==10
    assert DOC['url'] in totals['source_note']

@pytest.mark.parametrize('field,value',[
    ('source_id','made-up'),('calories',6000),('protein_g',float('nan')),
    ('portion_factor',-1),('portion_factor',float('inf')),('portion_factor',True),
    ('row_quote','Margherita 601 20'),('header_quote','Calories kcal only'),
    ('serving_quote','each slice is 100g'),('item','Other pizza')])
def test_invalid_evidence_cannot_be_published(field,value):
    result=copy.deepcopy(RESULT); result['evidence'][0][field]=value
    assert nr.published_totals(result,[DOC]) is None

@pytest.mark.parametrize('field',['market_and_variant_match','all_components_matched'])
def test_foreign_market_or_incomplete_combo_is_not_match(field):
    result=dict(RESULT,**{field:False})
    assert nr.published_totals(result,[DOC]) is None

@pytest.mark.parametrize('restaurant', ['Dominos','Taco Bell','Pizza Hut','KFC','Meghana','Nandhana','An Unknown Local Cafe'])
def test_any_named_restaurant_triggers_search(monkeypatch,restaurant):
    initial=dict(is_food=True,restaurant=restaurant,country='India',dish_names='one sandwich',
                 needs_clarification=False,calories=400,protein_g=15)
    search=Mock(return_value=dict(search_ok=True,documents=[]))
    monkeypatch.setattr(web,'research',search)
    result=nr.estimate('Lunch','food description',initial)
    search.assert_called_once_with(restaurant,'one sandwich','India')
    assert 'not verified brand nutrition' in result['_research_note']


def test_rejected_grounding_uses_initial_recipe_not_fabricated_total(monkeypatch):
    monkeypatch.setattr(web,'research',Mock(return_value=dict(search_ok=True,documents=[DOC])))
    bad=copy.deepcopy(RESULT); bad['evidence'][0]['calories']=1
    monkeypatch.setattr(nvidia_client,'analyze_sources',Mock(return_value=bad))
    result=nr.estimate('Lunch','half pizza',dict(is_food=True,restaurant='New cafe',calories=350,protein_g=12))
    assert result['calories']==350 and not result.get('_published_match')


def test_search_failure_is_honest(monkeypatch):
    monkeypatch.setattr(web,'research',Mock(return_value=dict(search_ok=False,documents=[])))
    result=nr.estimate('Lunch','burger',dict(is_food=True,restaurant='New cafe',calories=450,protein_g=20))
    assert 'unavailable' in result['_research_note']


def test_restaurant_test_mode_does_not_write(ctx,monkeypatch):
    monkeypatch.setattr(config,'CALORIE_MULTIPLIER',1)
    monkeypatch.setattr(nvidia_client,'estimate',Mock(return_value=dict(is_food=True,restaurant='New cafe',dish_names='pizza')))
    monkeypatch.setattr(nvidia_client,'verify_source_match',Mock(return_value=dict(verified=True,calories=300,protein_g=10)))
    search=Mock(return_value=dict(search_ok=True,documents=[DOC]));monkeypatch.setattr(web,'research',search)
    monkeypatch.setattr(nvidia_client,'analyze_sources',Mock(return_value=RESULT))
    food='New cafe, 2 slices of personal pan Margherita pizza'
    db.set_cached_food(db.food_cache_key(food),123,5,'veg','old automatic guess')
    asyncio.run(bot.finalize_entry('42',bot.today_local().isoformat(),'morning',food,ctx,False,test_mode=True))
    assert '300 kcal' in ctx.bot.send_message.call_args.kwargs['text']
    assert db.get_cached_food(db.food_cache_key(food))['calories']==123
    bot.sheets_client.append_entry.assert_not_called()
    bot.nutrition_api.lookup.assert_not_called()
    search.assert_called_once()

@pytest.mark.parametrize('url',['file:///etc/passwd','http://user:pass@example.com','http://localhost:22/a','http://127.0.0.1','http://169.254.169.254/metadata','http://[::1]'])
def test_private_and_non_web_targets_blocked(url,monkeypatch):
    if '127.' in url or '169.254.' in url or '[::1]' in url:
        host=web.urlsplit(url).hostname
        monkeypatch.setattr(web.socket,'getaddrinfo',lambda *a,**kw:[(None,None,None,None,(host,80))])
    with pytest.raises(ValueError):web.public_target(url)


def test_mixed_public_private_dns_is_blocked(monkeypatch):
    monkeypatch.setattr(web.socket,'getaddrinfo',lambda *a,**kw:[(None,None,None,None,('8.8.8.8',443)),(None,None,None,None,('10.0.0.1',443))])
    with pytest.raises(ValueError):web.public_target('https://example.com')


def test_redirect_rechecks_destination(monkeypatch):
    monkeypatch.setattr(web.socket,'getaddrinfo',lambda host,*a,**kw:[(None,None,None,None,('127.0.0.1' if host=='localhost' else '8.8.8.8',80))])
    response=Mock(status=302,headers={'Location':'http://localhost/secret'})
    pool=Mock();pool.urlopen.return_value=response
    monkeypatch.setattr(web.urllib3,'HTTPConnectionPool',Mock(return_value=pool))
    with pytest.raises(ValueError):web.fetch('http://example.com')
    assert pool.urlopen.call_count==1


def test_oversize_content_blocked(monkeypatch):
    monkeypatch.setattr(web,'public_target',lambda url:(web.urlsplit(url),80,'8.8.8.8'))
    response=Mock(status=200,headers={'Content-Length':str(web.MAX_BYTES+1)})
    pool=Mock();pool.urlopen.return_value=response
    monkeypatch.setattr(web.urllib3,'HTTPConnectionPool',Mock(return_value=pool))
    with pytest.raises(ValueError):web.fetch('http://example.com')
    response.read.assert_not_called()


def test_third_party_nutrition_blog_cannot_be_published():
    assert nr.published_totals(RESULT,[dict(DOC,source_class='unverified_publisher')]) is None

@pytest.mark.parametrize('url,restaurant,expected',[
    ('https://about.kfc.co.in/menu.pdf','KFC India','primary_candidate'),
    ('https://www.tacobell.co.in/nutrition','Taco Bell','primary_candidate'),
    ('https://tacobellpricesguide.com/nutrition','Taco Bell','unverified_publisher'),
    ('https://www.yourtrainer.in/kfc','KFC','unverified_publisher'),
    ('https://tacobell.evil.com/nutrition','Taco Bell','unverified_publisher'),
    ('https://www.newlocalcafe.in/menu','New Local Cafe','primary_candidate')])
def test_source_domain_is_not_a_brand_substring(url,restaurant,expected):
    assert web.source_class(url,restaurant)==expected


def test_second_check_disagrees_on_protein_column(monkeypatch):
    monkeypatch.setattr(web,'research',Mock(return_value=dict(search_ok=True,documents=[DOC])))
    monkeypatch.setattr(nvidia_client,'analyze_sources',Mock(return_value=RESULT))
    monkeypatch.setattr(nvidia_client,'verify_source_match',Mock(return_value=dict(verified=True,calories=300,protein_g=20)))
    initial=dict(is_food=True,restaurant='New cafe',calories=350,protein_g=15)
    result=nr.estimate('Lunch','half pizza',initial)
    assert not result.get('_published_match') and result['calories']==350


def test_source_analysis_failure_is_a_labelled_recipe_estimate(monkeypatch):
    monkeypatch.setattr(web,'research',Mock(return_value=dict(search_ok=True,documents=[DOC])))
    monkeypatch.setattr(nvidia_client,'analyze_sources',Mock(side_effect=TimeoutError))
    result=nr.estimate('Lunch','half pizza',dict(is_food=True,restaurant='New cafe',calories=350,protein_g=15))
    assert result['calories']==350 and not result.get('_published_match')
    assert 'not verified brand nutrition' in result['_research_note']


def test_unverified_second_check_cannot_publish(monkeypatch):
    monkeypatch.setattr(web,'research',Mock(return_value=dict(search_ok=True,documents=[DOC])))
    monkeypatch.setattr(nvidia_client,'analyze_sources',Mock(return_value=RESULT))
    monkeypatch.setattr(nvidia_client,'verify_source_match',Mock(side_effect=TimeoutError))
    result=nr.estimate('Lunch','half pizza',dict(is_food=True,restaurant='New cafe',calories=350,protein_g=15))
    assert not result.get('_published_match')


def test_html_follows_nutrition_pdf_and_ignores_self_link():
    text,links=web.extract('https://example.com/nutrition','text/html',b'<h1>Nutrition</h1><a href="#nutrition">Nutrition</a><a href="/assets/nutrition.pdf">Nutrition PDF</a>')
    assert links==['https://example.com/assets/nutrition.pdf']


def test_research_does_not_treat_search_snippets_as_evidence(monkeypatch):
    engine=Mock();engine.text.return_value=[dict(href='https://cafe.example.com/food',body='500 calories 20 g protein')]
    monkeypatch.setattr(web,'DDGS',Mock(return_value=engine))
    monkeypatch.setattr(web,'fetch',Mock(side_effect=ValueError('blocked')))
    result=web.research('Cafe','sandwich')
    assert result['search_ok'] and result['documents']==[]


def test_india_search_always_uses_country_domain_even_with_global_brand(monkeypatch):
    engine=Mock();engine.text.return_value=[dict(href='https://www.tacobell.com/nutrition')]
    monkeypatch.setattr(web,'DDGS',Mock(return_value=engine))
    monkeypatch.setattr(web,'fetch',Mock(side_effect=ValueError('blocked')))
    web.research('Taco Bell','taco','India')
    queries=[call.args[0] for call in engine.text.call_args_list]
    assert any('site:in' in query for query in queries)


def test_domestic_primary_sources_rank_before_us_pdf(monkeypatch):
    engine=Mock();engine.text.return_value=[dict(href='https://www.tacobell.com/us.pdf'),dict(href='https://www.tacobell.co.in/nutrition')]
    monkeypatch.setattr(web,'DDGS',Mock(return_value=engine))
    fetch=Mock(side_effect=ValueError('blocked'));monkeypatch.setattr(web,'fetch',fetch)
    web.research('Taco Bell','taco','India')
    assert fetch.call_args_list[0].args[0]=='https://www.tacobell.co.in/nutrition'


def test_cached_source_fallback_preserves_original_date(monkeypatch):
    import source_cache
    url='https://www.tacobell.co.in/menu.pdf'
    source_cache.save(url,'Nutrition facts '+('food calories protein ' * 10),[],'2026-10-18')
    engine=Mock();engine.text.return_value=[dict(href=url)]
    monkeypatch.setattr(web,'DDGS',Mock(return_value=engine))
    monkeypatch.setattr(web,'fetch',Mock(side_effect=ValueError('403')))
    result=web.research('Taco Bell','taco','India')
    assert result['documents'][0]['cached'] is True
    assert result['documents'][0]['retrieved_at']=='2026-10-18'
    engine.text.assert_called()  # A saved page never skips fresh searching.


def test_expired_public_page_is_not_reused(monkeypatch):
    import source_cache
    url='https://example.com/nutrition'
    source_cache.save(url,'nutrition '*20,[],'2026-10-18')
    now=source_cache.time.time()
    monkeypatch.setattr(source_cache.time,'time',lambda:now+source_cache.MAX_AGE_SECONDS+1)
    assert source_cache.read(url) is None


def test_cached_published_match_discloses_date_and_fallback():
    note=nr.published_totals(RESULT,[dict(DOC,cached=True,retrieved_at='2026-10-18')])['source_note']
    assert '2026-10-18' in note and 'saved public page' in note


def test_pdf_spacing_differences_do_not_discard_exact_numeric_evidence():
    doc=dict(DOC,text=DOC['text'].replace('Margherita 600 20','Margherita600 20'))
    assert nr.published_totals(RESULT,[doc])['calories']==300


def test_explicit_us_pdf_is_not_used_for_india():
    assert web.foreign_market('https://cache.dominos.com/assets/market/US/en/menu.pdf','India')
    assert not web.foreign_market('https://www.dominos.co.in/nutrition.pdf','India')
    assert not web.foreign_market('https://cache.dominos.com/assets/market/US/en/menu.pdf','USA')
