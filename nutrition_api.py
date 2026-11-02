import json
import logging
import math

import requests

import config

logger = logging.getLogger("calorie-bot.nutrition_api")


def nutrient(value):
    """Accept numeric strings without turning missing nutrients into zero."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def lookup(query: str):
    """Return complete numeric totals, or None (including free-tier placeholders)."""
    if not config.NUTRITION_API_KEY:
        return None
    try:
        resp = requests.get(
            config.NUTRITION_API_URL, params={"query": query},
            headers={"X-Api-Key": config.NUTRITION_API_KEY}, timeout=8,
        )
        resp.raise_for_status()
        items = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Nutrition API lookup unavailable (%s)", type(exc).__name__)
        return None

    logger.info("Nutrition API response: %s", json.dumps(items, ensure_ascii=True)[:300])
    if not isinstance(items, list) or not items:
        return None
    total_cal = total_protein = 0.0
    for item in items:
        if not isinstance(item, dict):
            return None
        calories = nutrient(item.get("calories"))
        protein = nutrient(item.get("protein_g"))
        if calories is None or protein is None:
            logger.info("Nutrition database not usable: missing/non-numeric nutrients; using AI")
            return None
        total_cal += calories
        total_protein += protein
    return (round(total_cal, 1), round(total_protein, 1)) if total_cal > 0 else None
