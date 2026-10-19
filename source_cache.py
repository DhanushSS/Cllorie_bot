"""Short-lived cache of public page text, separate from personal meal state."""
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urldefrag

MAX_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_TEXT = 180000


def cache_dir():
    return Path(os.environ.get('NUTRITION_SOURCE_CACHE_DIR', str(Path(os.environ.get('DB_PATH', 'bot_state.db')).parent / 'nutrition_sources')))


def key(url):
    return hashlib.sha256(urldefrag(url)[0].rstrip('/').encode()).hexdigest() + '.json'


def save(url, text, links, retrieved_at):
    if not 100 <= len(text) <= MAX_TEXT:
        return
    directory = cache_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / key(url)
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(url=url, text=text, links=links[:3], retrieved_at=retrieved_at, saved_at=time.time())))
        temporary.replace(target)
        # Keep public data bounded; never touch the user's meal database.
        files = sorted(directory.glob('*.json'), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in files[100:]:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def read(url):
    try:
        path = cache_dir() / key(url)
        if path.stat().st_size > 2 * 1024 * 1024:
            return None
        data = json.loads(path.read_text())
        age = time.time() - float(data['saved_at'])
        if not 0 <= age <= MAX_AGE_SECONDS or key(data['url']) != key(url) or not 100 <= len(data['text']) <= MAX_TEXT:
            return None
        if not isinstance(data['links'], list) or not isinstance(data['retrieved_at'], str):
            return None
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None
