"""Food facts and serving checks that should not depend on an LLM guess."""
import re


def user_text(text):
    return re.sub(r'Answered question:.*?Clarification:', '', text, flags=re.I | re.S).casefold()


def category(text):
    text = user_text(text)
    # Plant-based substitutes are not animal meat; remove explicitly excluded meat.
    if re.search(r'\b(?:vegan|plant[- ]based|mock|soya|soy)\s+(?:chicken|meat|fish)\b', text):
        return 'veg'
    text = re.sub(r'\b(?:no|without)\s+(?:chicken|mutton|meat|fish|egg|prawns?)\b', '', text)
    if re.search(r'\b(?:chicken|chkn|mutton|lamb|beef|pork|fish|prawns?|shrimp|seafood|eggs?|non[- ]?veg)\b', text):
        return 'non-veg'
    if re.search(r'\b(?:veg|vegetarian|vegan|paneer|mushroom|aloo)\b', text):
        return 'veg'
    return None


def question(text, options=(), kind='portion', category_value='unknown'):
    return dict(is_food=True, intent='food', needs_clarification=True,
                clarification_kind=kind, clarification_question=text,
                clarification_options=list(options), category=category_value,
                calories=None, protein_g=None)


def biryani_preflight(text):
    value = user_text(text)
    if not re.search(r'\b(?:biryani|biriyani|biriani)\b', value):
        return None
    diet = category(text)
    if not diet:
        return question('Which biryani was it?', ['Chicken', 'Mutton', 'Veg', 'Egg'], 'diet')
    # Counting one order is different from knowing the amount eaten. A small home
    # serving and a restaurant box must not share an assumed one-cup portion.
    clear_size = re.search(r'\b\d+(?:\.\d+)?\s*(?:g|gm|grams?|kg|cups?|ml)\b|\b(?:one|two|half|a)\s+cup\b|\b(?:full|whole|half|quarter)\s+(?:restaurant\s+)?(?:order|box|pack|portion|plate)\b|\b(?:small|medium|large)\s+(?:plate|bowl)\b', value)
    if not clear_size:
        return question("Got it. How much biryani did you eat? One restaurant order can be much bigger than a cup.",
                        ['Full restaurant order', 'Half restaurant order', 'One cup', 'I know the weight'], category_value=diet)
    return None


def is_redundant_diet_question(result, text):
    if not category(text) or not result.get('needs_clarification'):
        return False
    q = str(result.get('clarification_question') or '').casefold()
    return result.get('clarification_kind') == 'diet' or ('veg' in q and ('non' in q or 'vegetarian' in q))
