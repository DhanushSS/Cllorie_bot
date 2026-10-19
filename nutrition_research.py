"""Restaurant-neutral search orchestration and evidence-checked arithmetic."""
import logging
import math
import re

import nvidia_client
import web_nutrition

logger = logging.getLogger('calorie-bot.research')


def normalized(value):
    return re.sub(r'\s+', ' ', str(value)).strip().casefold()


def number(value):
    if isinstance(value, bool):
        raise ValueError('Boolean nutrient')
    n = float(value)
    if not math.isfinite(n) or n < 0:
        raise ValueError('Invalid nutrient')
    return n


def published_totals(result, documents):
    """Only calculate totals from cited numeric rows present in fetched documents.

    Semantic dish/market/serving matching remains model-assisted, not a measured
    guarantee of restaurant recipe accuracy. No model-provided URL is displayed.
    """
    if result.get('basis') != 'published_match' or result.get('all_components_matched') is not True or result.get('market_and_variant_match') is not True:
        return None
    evidence = result.get('evidence')
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 5:
        return None
    sources = {doc['id']: doc for doc in documents}
    calories = protein = 0
    cited = []
    portions = []
    try:
        for item in evidence:
            doc = sources[item['source_id']]
            if doc.get('source_class') not in ('primary_candidate', 'merchant'):
                return None
            text = normalized(doc['text'])
            for key in ('row_quote', 'header_quote', 'serving_quote'):
                quote = normalized(item[key])
                if len(quote) < 4 or len(quote) > 1800 or quote not in text:
                    return None
            header = normalized(item['header_quote'])
            if not re.search(r'kcal|calori|energy', header) or 'protein' not in header:
                return None
            row_numbers = {float(x.replace(',', '')) for x in re.findall(r'(?<!\w)\d[\d,]*(?:\.\d+)?', item['row_quote'])}
            cal, pro, factor = number(item['calories']), number(item['protein_g']), number(item['portion_factor'])
            if cal not in row_numbers or pro not in row_numbers or not 0 < factor <= 20 or not 0 < cal <= 15000 or pro > 1000 or pro * 4 > cal * 1.1:
                return None
            if not normalized(item.get('item')) or normalized(item['item']) not in normalized(item['row_quote']):
                return None
            explanation = str(item.get('portion_explanation', '')).strip()
            if not explanation:
                return None
            calories += cal * factor
            protein += pro * factor
            cited.append(doc)
            portions.append(explanation[:250])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    urls = list(dict.fromkeys(doc['url'] for doc in cited))
    note = 'Published menu match; scaled to your portion. ' + ' '.join(portions)
    note += '\nSources checked ' + cited[0]['retrieved_at'] + ':\n' + '\n'.join(urls[:3])
    note += '\nRestaurant preparation can vary; ±10% accuracy is not guaranteed.'
    return dict(calories=round(calories, 1), protein_g=round(protein, 1), source_note=note)


def estimate(meal_label, food_text, initial=None):
    result = initial if initial is not None else nvidia_client.estimate(meal_label, food_text)
    restaurant = result.get('restaurant')
    if result.get('is_food') is not True or not isinstance(restaurant, str) or not restaurant.strip():
        return result
    country = result.get('country') or 'India'
    dish = result.get('dish_names') or ''
    if not isinstance(dish, str):
        dish = ' '.join(map(str, dish)) if isinstance(dish, list) else ''
    research = web_nutrition.research(restaurant, dish, str(country))
    docs = research['documents']
    logger.info('Restaurant research completed: search_ok=%s documents=%d', research['search_ok'], len(docs))
    candidate = result
    if docs:
        try:
            candidate = nvidia_client.analyze_sources(meal_label, food_text, restaurant, country, docs)
            if candidate.get('is_food') is not True:
                candidate = result
        except Exception as exc:
            logger.warning('Source analysis unavailable (%s)', type(exc).__name__)
            candidate = result
    candidate = dict(candidate, restaurant=restaurant, country=country, _restaurant_researched=True)
    if candidate.get('needs_clarification'):
        return candidate
    matched = published_totals(candidate, docs)
    if matched:
        try:
            check = nvidia_client.verify_source_match(food_text, candidate, [doc for doc in docs if doc.get('source_class') in ('primary_candidate', 'merchant')])
            if check.get('verified') is not True or abs(number(check['calories']) - matched['calories']) > 0.2 or abs(number(check['protein_g']) - matched['protein_g']) > 0.2:
                matched = None
        except Exception:
            matched = None
    if matched:
        candidate.update(matched)
        candidate['_published_match'] = True
        return candidate
    # Never reuse a claimed published total after evidence validation failed.
    if candidate.get('basis') == 'published_match':
        candidate = dict(result, restaurant=restaurant, country=country, _restaurant_researched=True)
    # Unverified analyses must not claim official values in free-form notes.
    if candidate is not result and not candidate.get('needs_clarification'):
        candidate['notes'] = 'Recipe-based estimate for the stated food and portion.'
    if not research['search_ok']:
        status = 'Web search is unavailable right now.'
    elif not docs:
        status = "I searched, but couldn't read a usable nutrition page."
    else:
        status = "I searched, but couldn't verify a matching published value for this exact meal and portion."
    candidate['_research_note'] = status + ' This is a recipe estimate, not verified brand nutrition; ±10% accuracy is not guaranteed.'
    if docs:
        candidate['_research_note'] += '\nPages checked (not confirmed nutrition matches):\n' + '\n'.join(doc['url'] for doc in docs[:2])
    return candidate
