"""Small, predictable chat navigation; food interpretation stays with the estimator."""
import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

HELP_TEXT = (
    "Hey 👋 I can help you keep track of calories and protein.\n\n"
    "Tell me what you ate and roughly how much — ‘2 idlis and a bowl of sambar’ works. "
    "I'll ask if the food or portion isn't clear.\n\n"
    "Here's what you can do:\n"
    "• Log meal — save food to your daily log.\n"
    "• Test food (/test) — try an estimate without saving anything.\n"
    "• Today / Week / Month — see your totals; week and month include a chart.\n"
    "• Edit meal — replace something you logged today.\n"
    "• Correct totals — give known calories and protein; I remember the correction.\n"
    "• Skip meal — move past the next unlogged meal.\n"
    "• My usuals — reuse meals you've asked me to remember.\n"
    "• /fix morning 450 20 — correct breakfast to 450 kcal and 20 g protein.\n"
    "• /cancel — leave test mode or cancel a follow-up.\n\n"
    "Your log goes breakfast → lunch → dinner. Estimates depend on the portion, "
    "so tell me if you had half, extra, or a different amount."
)


def menu_keyboard():
    return ReplyKeyboardMarkup([
        ['Log meal', 'Test food'], ['Today', 'Week', 'Month'],
        ['Edit meal', 'Correct totals', 'Skip meal'], ['My usuals', 'Help', 'Cancel'],
    ], resize_keyboard=True, is_persistent=True, input_field_placeholder='What did you have?')


def normalize(text):
    return ' '.join(re.sub(r'[^\w\s]', ' ', text.casefold()).split())


def intent(text):
    value = normalize(text)
    routes = {
        'help': 'help', 'menu': 'help', 'what can you do': 'help',
        'show me the menu': 'help', 'how does this work': 'help',
        'today': 'today', 'show today': 'today', 'my totals': 'today',
        'how much did i eat today': 'today', 'week': 'week', 'weekly report': 'week',
        'month': 'month', 'monthly report': 'month', 'test food': 'test',
        'test': 'test', 'try an estimate': 'test', 'log meal': 'log',
        'edit meal': 'edit', 'edit': 'edit', 'correct totals': 'fix', 'skip meal': 'skip', 'skip': 'skip',
        'my usuals': 'usuals', 'usuals': 'usuals', 'remember this': 'remember',
        'cancel': 'cancel', 'never mind': 'cancel', 'nevermind': 'cancel',
        'stop testing': 'cancel', 'exit test': 'cancel',
        'thanks': 'thanks', 'thank you': 'thanks', 'thank u': 'thanks',
        'ty': 'thanks', 'okay': 'ack', 'ok': 'ack', 'cool': 'ack',
        'bye': 'bye', 'good night': 'bye',
    }
    if value in routes:
        return routes[value]
    greeting = r'(?:h+i+|he+y+|hello+|yo+|namaste|vanakkam|good morning|good afternoon|good evening|gud mrng)'
    addressee = r'(?: there| bot| bro| dude| buddy| mate)?'
    followup = r'(?: how (?:are you|r u|you doing)| what s up)?'
    if re.fullmatch(greeting + addressee + followup, value) or value in ('how are you', 'how r u', 'what s up'):
        return 'greeting'
    return None


def is_conversation(text):
    original = re.split(r'\.\s*(?:Answered question:|Clarification:|It is (?:veg|non-veg)\.)', text, maxsplit=1, flags=re.I)[0]
    return intent(original) is not None


def test_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('Try a banana', callback_data='test_sample_banana'),
         InlineKeyboardButton('Try biryani', callback_data='test_sample_biryani')],
        [InlineKeyboardButton('Back to logging', callback_data='nav_log')],
    ])


def clarification_keyboard(options, scope, question_id):
    rows = [[InlineKeyboardButton(option, callback_data=f'answer:{scope}:{question_id}:{i}')]
            for i, option in enumerate(options)]
    rows.append([InlineKeyboardButton('Cancel', callback_data='nav_cancel')])
    return InlineKeyboardMarkup(rows)


def result_keyboard(test_mode=False, memory_id=None):
    if test_mode:
        return test_keyboard()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('Today’s totals', callback_data='nav_today'),
         InlineKeyboardButton('Edit meal', callback_data='nav_edit')],
        [InlineKeyboardButton('Correct totals', callback_data='nav_fix'),
         InlineKeyboardButton('Remember this meal', callback_data=f'remember:{memory_id}')],
    ])


def requires_portion(text):
    """Conservative check for common dishes whose serving size varies widely.

    Ignore the bot's question: only user descriptions and answers supply amounts.
    This supplements, rather than replaces, the model's broader food validation.
    """
    user_text = re.sub(r'Answered question:.*?Clarification:', '', text, flags=re.I | re.S)
    variable_food = re.search(r'\b(?:biryani|rice|curry|dal|dhal|sambar|pasta|noodles|soup|chicken|fish|paneer|idlis?|chapatis?|rotis?)\b', user_text, re.I)
    quantity = re.search(r'\b\d+(?:\.\d+)?\s*(?:g|gm|grams?|kg|ml|l|cups?|bowls?|plates?|pieces?|servings?|idlis?|chapatis?|rotis?|bananas?|eggs?)\b|\b(?:one|two|three|four|five|six|half|quarter)\b|\b(?:a|an)\s+(?:small |medium |large )?(?:bowl|cup|plate|piece|serving)\b', user_text, re.I)
    # Accept standalone counts after a clarification as well.
    quantity = quantity or re.search(r'Clarification:\s*\d+(?:\.\d+)?\s*$', text, re.I)
    return bool(variable_food and not quantity)


def diet_is_stated(text):
    user_text = re.sub(r'Answered question:.*?Clarification:', '', text, flags=re.I | re.S)
    return bool(re.search(r'\b(?:veg|vegetarian|non[ -]?veg|chicken|mutton|fish|egg|paneer)\b', user_text, re.I))
