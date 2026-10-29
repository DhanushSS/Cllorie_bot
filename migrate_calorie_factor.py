"""Offline, one-time migration for the 2026-10-19 switch to raw estimates.

Stop polling before running. Does not alter meal history or external Sheets.
"""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MARKER = 'migration:unadjusted-calories-20261019'


def migrate(path, old_factor=1.3, new_factor=1.0):
    if old_factor <= 0 or new_factor <= 0:
        raise ValueError('Factors must be positive')
    conn = sqlite3.connect(path)
    try:
        if conn.execute('SELECT value FROM kv WHERE key=?', (MARKER,)).fetchone():
            return {'already_applied': True, 'corrected_cache_rows': 0}
        backup = str(path) + '.before-unadjusted-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        with sqlite3.connect(backup) as target:
            conn.backup(target)
        os.chmod(backup, 0o600)
        with conn:
            changed = conn.execute("UPDATE food_cache SET calories=calories*? WHERE notes='manually corrected'", (old_factor / new_factor,)).rowcount
            conn.execute('INSERT INTO kv(key,value) VALUES(?,?)', (MARKER, f'{old_factor}->{new_factor}'))
        return {'already_applied': False, 'corrected_cache_rows': changed, 'backup': backup}
    finally:
        conn.close()


if __name__ == '__main__':
    import config
    if config.CALORIE_MULTIPLIER != 1.0:
        raise SystemExit('Set CALORIE_MULTIPLIER=1.0 before running this migration.')
    if not Path(config.DB_PATH).is_file():
        raise SystemExit('Existing database not found; refusing to create one.')
    print(migrate(config.DB_PATH))
