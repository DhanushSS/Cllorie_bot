import json
import re

from openai import OpenAI

import config

client = OpenAI(base_url=config.NVIDIA_BASE_URL, api_key=config.NVIDIA_API_KEY)

SYSTEM_PROMPT = """You are a nutrition estimation assistant for a personal food diary (mostly Indian home-cooked \
and restaurant food, but handle any cuisine).

Given a short description of a meal, estimate total calories (kcal) and protein (grams) for what was most likely \
eaten.

Respond with ONLY a single JSON object, no markdown formatting, no extra commentary, in exactly this schema:
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
"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    return json.loads(text)


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
        max_tokens=400,
    )
    raw = resp.choices[0].message.content or ""

    try:
        data = _extract_json(raw)
    except Exception:
        data = {
            "needs_clarification": False,
            "clarification_question": None,
            "calories": None,
            "protein_g": None,
            "category": "unknown",
            "notes": "Couldn't parse the estimate cleanly — use /edit to correct this entry if needed.",
        }

    data.setdefault("needs_clarification", False)
    data.setdefault("clarification_question", None)
    data.setdefault("calories", None)
    data.setdefault("protein_g", None)
    data.setdefault("category", "unknown")
    data.setdefault("notes", "")
    return data
