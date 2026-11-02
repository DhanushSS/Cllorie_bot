"""Create a consistent recovery snapshot inside the running container.

Invoked by backup_to_github.sh. Never print meal data or credentials.
"""
import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config
import sheets_client

folder = Path('/data/recovery-stage')
folder.mkdir(mode=0o700, exist_ok=True)
with sqlite3.connect(config.DB_PATH) as src, sqlite3.connect(folder / 'state.db') as dst:
    src.backup(dst)
    assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
rows = sheets_client._sheet().get_all_values()
if not rows or rows[0] != sheets_client.HEADER:
    raise RuntimeError('Unexpected Sheet header; backup not published')
with (folder / 'owner-sheet.csv').open('w', newline='') as output:
    csv.writer(output).writerows(rows)
(folder / 'snapshot.json').write_text(json.dumps({
    'created_utc': datetime.now(timezone.utc).isoformat(),
    'sheet_rows': len(rows) - 1, 'database_integrity': 'ok',
    'format_version': 1,
}, indent=2))
for path in folder.iterdir():
    path.chmod(0o600)
print('Database and Sheet snapshot complete')
