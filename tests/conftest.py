import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

# All test services are mocked; never load developer credentials.
os.environ.update(TELEGRAM_BOT_TOKEN='123:test-only', NVIDIA_API_KEY='test-only-nvidia',
                  GOOGLE_SERVICE_ACCOUNT_JSON='{}', GOOGLE_SHEET_ID='test-only-sheet',
                  NUTRITION_API_KEY='test-only-nutrition', ALLOWED_CHAT_ID='',
                  CALORIE_MULTIPLIER='1.3', TIMEZONE='Asia/Kolkata')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import bot
import config
import db


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DB_PATH', str(tmp_path / 'data' / 'state.db'))
    monkeypatch.setattr(config, 'ALLOWED_CHAT_ID', '')
    monkeypatch.setattr(bot.sheets_client, 'append_entry', Mock())
    monkeypatch.setattr(bot.sheets_client, 'update_entry', Mock())
    monkeypatch.setattr(bot.nutrition_api, 'lookup', Mock(return_value=None))
    monkeypatch.setattr(bot.nvidia_client, 'estimate', Mock(return_value={
        'is_food':True,'needs_clarification': False, 'calories': 100, 'protein_g': 4, 'category': 'veg'}))
    db.set_kv('chat_id', '42')


@pytest.fixture
def ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock()), args=[])


@pytest.fixture
def update():
    return SimpleNamespace(effective_chat=SimpleNamespace(id=42),
                           message=SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock(), text='banana'),
                           callback_query=None)
