"""Curated, dated menu facts. Never infer one brand's nutrients from another.

Menu energies are interpreted as the listed menu item, not per 100 g. This basis
is displayed explicitly; side nutrition is not separately itemized by the source.
"""
from datetime import date, timedelta
import re
import food_rules

CHECKED = date(2026, 10, 19)
SOURCES = {
    'meghana': 'https://www.swiggy.com/city/bangalore/meghana-foods-residency-road-rest3241?query=Chicken',
    'nandhana': 'https://www.swiggy.com/city/bangalore/nandhana-palace-beml-layout-5th-stage-banashankari-rest389325',
}
# Names, nutrients and serving descriptions transcribed from the merchant menus.
ITEMS = {
    'meghana': {
        'regular': ('Chicken Biryani', 1615.27, 61.82, 'non-veg', 'described as serving two'),
        'boneless': ('Chicken Boneless Biryani', 1446.5, 64.22, 'non-veg', 'described as serving two'),
        'special': ('Meghana Special Boneless Biryani', 1456.21, 64.65, 'non-veg', 'described as serving two'),
        'paneer': ('Paneer Biryani', 1800.51, 61.42, 'veg', 'described as serving two'),
        'veg': ('Spl Veg Biryani', 1327.35, 26.99, 'veg', 'described as serving two'),
    },
    'nandhana': {
        'supreme': ('Supreme Boneless Chicken Biryani', 1455, 76.2, 'non-veg', 'listed standard menu item'),
        'hyderabadi': ('Hyderabadi Chicken Biryani', 1290, 52.2, 'non-veg', 'listed standard menu item'),
        'nellore': ('Nellore Chicken Biryani', 1012.5, 17.25, 'non-veg', 'listed as 800 g, serving one to two'),
    },
}


def brand(text):
    value = food_rules.user_text(text)
    if re.search(r'\bmegha?na(?:s)?\b', value):
        return 'meghana'
    if re.search(r'\bnandh?ana(?:s)?\b', value):
        return 'nandhana'
    return None


def matched_item(which, text):
    value = food_rules.user_text(text)
    # These are different products, not valid substitutions for catalog entries.
    if re.search(r'\b(?:bucket|family|jumbo|555|donne|high protein|fry|mutton|prawns?|fish|eggs?|mushroom|aloo|manchurian)\b', value):
        return None
    if which == 'meghana':
        if re.search(r'\b(?:veg|vegetarian)\b', value):
            return 'paneer' if 'paneer' in value else 'veg'
        for key in ('paneer', 'special', 'boneless'):
            if re.search(r'\b' + key + r'\b', value): return key
        if re.search(r'\b(?:veg|vegetarian)\b', value): return 'veg'
        if re.search(r'\b(?:chicken|chkn|regular)\b', value): return 'regular'
    if which == 'nandhana':
        for key in ('supreme', 'hyderabadi', 'nellore'):
            if re.search(r'\b' + key + r'\b', value): return key
        if 'boneless' in value: return 'supreme'
    return None


def order_fraction(text):
    value = food_rules.user_text(text)
    latest = re.split(r'Clarification:', text, flags=re.I)[-1].strip().casefold()
    if 'Clarification:' in text and latest in ('full', 'whole', 'half', 'quarter'):
        return {'full': 1., 'whole': 1., 'half': .5, 'quarter': .25}[latest]
    # Require an explicit order fraction; 'one biryani' alone is not a serving basis.
    if re.search(r'\b(?:quarter|1/4)\s+(?:of\s+(?:the\s+)?)?(?:(?:listed|restaurant)\s+)?order\b', value): return .25
    if re.search(r'\b(?:half|1/2)\s+(?:of\s+(?:the\s+)?)?(?:(?:listed|restaurant)\s+)?order\b', value): return .5
    if re.search(r'\b(?:full|whole)\s+(?:(?:listed|restaurant)\s+)?order\b', value): return 1.0
    return None


def simple_menu_description(value):
    # Conservative matching: unknown additions/qualifiers go to the reasoning
    # estimator, rather than silently dropping part of the user's meal.
    words = set(re.findall(r"[a-z]+", value))
    allowed = set("i had have ate eat eaten got ordered today yesterday for my a an one the of from at in it was is this that please log test food meal lunch dinner breakfast meghana meghna meghanas foods nandhana nandana nandhanas palace chicken chkn biryani biriyani biriani boneless regular special spl supreme hyderabadi nellore paneer veg vegetarian full whole half quarter listed restaurant order portion box pack with including its sides cooked all just only yes thanks thank you".split())
    return words <= allowed


def resolve(text, today=None):
    which = brand(text)
    value = food_rules.user_text(text)
    if not which or not re.search(r'\b(?:biryani|biriyani|biriani)\b', value): return None
    if not simple_menu_description(value): return None
    if re.search(r'\b(?:and|plus)\b|\+|\b(?:two|three|2|3)\s+(?:full\s+)?orders?\b', value):
        return None
    if 'meghana' in value and re.search(r'\bnandh?ana\b', value):
        return None
    if (today or date.today()) - CHECKED > timedelta(days=90):
        return None  # Old menu facts cannot silently remain authoritative forever.
    # Prefer an explicitly selected latest variant over an earlier menu label.
    parts = re.split(r'\.\s*Answered question:.*?Clarification:\s*', text, flags=re.I | re.S)
    latest = parts[-1].casefold().strip()
    if latest == 'veg / paneer':
        return food_rules.question('Paneer biryani or special veg biryani?', ['Paneer biryani', 'Special veg biryani'], 'food', 'veg')
    key = next((matched_item(which, part) for part in reversed(parts) if matched_item(which, part)), None)
    unsupported = re.search(r'\b(?:bucket|family|jumbo|555|donne|high protein|fry|mutton|prawns?|fish|eggs?|mushroom|aloo|manchurian|other variant)\b', value)
    if unsupported: return None
    if which == 'nandhana' and food_rules.category(text) == 'veg': return None
    if not key:
        options = ['Regular chicken', 'Boneless chicken', 'Special boneless', 'Veg / paneer'] if which == 'meghana' else ['Hyderabadi chicken', 'Nellore chicken', 'Supreme boneless', 'Other variant']
        if 'veg / paneer' in value:
            options = ['Paneer biryani', 'Special veg biryani']
        return food_rules.question(f"Which {which.title()} biryani was it? Their dishes have different nutrition.", options, 'food', food_rules.category(text) or 'unknown')
    # Excluded sides / different weights need a recipe estimate, not invented
    # subtraction from a published complete-item value.
    if re.search(r'\b(?:without|no|less|extra|double|more)\b', value): return None
    if re.search(r'\b(?:no sides|without sides|no raita|without raita|no salan|without salan|only rice|left (?:the )?chicken|different amount)\b', value): return None
    if re.search(r'\b\d+(?:\.\d+)?\s*(?:g|gm|grams?|kg|cups?)\b|\b(?:one|a|half)\s+cup\b', value): return None
    fraction = order_fraction(text)
    item, calories, protein, category, serving = ITEMS[which][key]
    if fraction is None:
        return food_rules.question(
            f"I found {which.title()}'s {item} menu values ({serving}). How much of that listed order did you eat, including its listed sides? If you left sides or had a different amount, tell me.",
            ['Full listed order', 'Half listed order', 'Quarter listed order', 'Different amount / no sides'], category_value=category)
    return dict(is_food=True, intent='food', needs_clarification=False,
                category=category, calories=round(calories*fraction, 1), protein_g=round(protein*fraction, 1),
                source_note=(f"Published menu listing: {which.title()} — {item}. {fraction:g} × listed item; {serving}. "
                             f"Assumes the listed item total; sides aren't itemized separately. Portions can vary by outlet. Checked {CHECKED.isoformat()}.\n{SOURCES[which]}"))
