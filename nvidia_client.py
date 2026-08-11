import json
import logging
import re

from openai import OpenAI

import config

logger = logging.getLogger("calorie-bot.nvidia")

client = OpenAI(base_url=config.NVIDIA_BASE_URL, api_key=config.NVIDIA_API_KEY)

SYSTEM_PROMPT = """You are a nutrition estimation assistant for a personal food diary (mostly Indian home-cooked \
and restaurant food, but handle any cuisine).

Given a short description of a meal, estimate total calories (kcal) and protein (grams) for what was most likely \
eaten.

Respond with ONLY a single JSON object and nothing else - no markdown formatting, no code fences, no reasoning, \
no <think> tags, no commentary before or after it. Your entire reply must be exactly this JSON schema:
{
  "needs_clarification": true or false,
  "clarification_question": "<short question, or null>",
  "calories": <number, or null>,
  "protein_g": <number, or null>,
  "category": "veg" or "non-veg" or "unknown",
  "notes": "<one short sentence about assumptions you made, e.g. portion size>"
}

Rules:
- If the dish could reasonably be veg OR non-veg and the text doesn't already make that clear (e.g. biryani, curry, \
sandwich, roll, fried rice, soup, pulao) set needs_clarification=true and ask ONE short question, e.g. \
"Veg or non-veg? Roughly how many plates/cups?". Leave calories/protein/category as null/unknown in that case.
- If quantity isn't mentioned, assume one standard home or restaurant serving and state that assumption in "notes"; \
don't ask a clarifying question just for missing quantity.
- If the incoming text already looks like it's answering a previous clarifying question (mentions veg/non-veg, or \
gives a quantity), incorporate it and set needs_clarification=false.
- Keep calorie and protein numbers realistic for typical portions.
- Never leave calories/protein null unless needs_clarification is true.

detailed thinking off
"""
# The "detailed thinking off" line suppresses this Nemotron model's internal reasoning
# trace at the prompt level. We also pass chat_template_kwargs/enable_thinking=False
# below as a second, more reliable way of disabling it for Nemotron 3.x models, and
# strip any <think>...</think> block defensively in case one still slips through.


def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _extract_json(text: str) -> dict:
    """Pull the first complete top-level JSON object out of text, tolerant of any
    stray prose/reasoning/code-fences around it (brace-matching, not naive regex)."""
    text = _strip_thinking(text)
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()

    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model output")

    depth = 0
    in_string = False
    escape = False
    end = None
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break

    if end is None:
        raise ValueError("unterminated JSON object in model output")

    return json.loads(text[start:end + 1])


def _fallback(reason: str) -> dict:
    return {
        "needs_clarification": False,
        "clarification_question": None,
        "calories": None,
        "protein_g": None,
        "category": "unknown",
        "notes": f"Couldn't parse the estimate cleanly ({reason}) — use /edit to correct this entry.",
    }


def estimate(meal_label: str, food_text: str) -> dict:
    """Ask the NVIDIA-hosted LLM to estimate calories/protein, or flag ambiguity."""
    user_msg = f"Meal: {meal_label}\nDescription: {food_text}"

    resp = client.chat.completions.create(
        model=config.NVIDIA_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
        max_tokens=300,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    raw = resp.choices[0].message.content or ""
    logger.info("NVIDIA raw response: %r", raw)

    try:
        data = _extract_json(raw)
    except Exception as e:
        logger.warning("Failed to parse NVIDIA response as JSON (%s). Raw: %r", e, raw)
        data = _fallback(str(e))

    data.setdefault("needs_clarification", False)
    data.setdefault("clarification_question", None)
    data.setdefault("calories", None)
    data.setdefault("protein_g", None)
    data.setdefault("category", "unknown")
    data.setdefault("notes", "")
    return data
