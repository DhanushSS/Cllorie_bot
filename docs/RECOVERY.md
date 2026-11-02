# Recovery independent of Azure

Code is versioned in this repository. Private state and credentials must never
be committed here. Encrypted snapshots are stored in the owner's separate private
`DhanushSS/cllorie-bot-backups` repository, using a repository-specific deploy key.
The Azure server has only the public encryption certificate. The separate private
recovery key is in the owner's local `CLLORIE_RECOVERY_KIT` output and must also be
saved somewhere independent of this Mac.

`backup_snapshot.py` takes a consistent SQLite snapshot with integrity checking,
and exports the owner Google Sheet as CSV. `backup_to_github.sh` bundles these
with private configuration and an exact source archive, then encrypts with
OpenSSL CMS AES-256-GCM before pushing. A failed snapshot/export/encryption/upload
does not mark the backup successful. The private repository README gives restore
steps. No bot stop is required for snapshotting.

The VM runs backups every six hours. If the VM or Azure subscription stops, the
last successfully pushed snapshot remains available on GitHub. New meals since
that snapshot may be lost. Public nutrition-page caches are regenerated and are
not backed up. The Google Sheet also remains an independent copy of owner logs.
All users' database records, reminders, corrections and usuals are included.

The installed systemd units are versioned under `ops/`. Reinstall them into
`/etc/systemd/system/` on a replacement VM and enable `calorie-backup.timer`
after configuring `backups/recovery-public.pem`, the repository-scoped
`backups/github_key`, pinned `backups/known_hosts`, and `backups/repository`.
Generate a new deploy key on the replacement host and authorize it only for the
private backup repository; the recovery private key should stay off the server.

On 2 November 2026 the snapshot was pushed, downloaded from GitHub, decrypted,
and checked for SQLite integrity, Sheet rows, configuration and source presence.
The installed service exited successfully and its six-hour timer was enabled.
This verified recovery-file readability; it did not start a second live poller.

Monitor `/opt/calorie-bot/backups/last-success` and
`/opt/calorie-bot/backups/backup.log`. Backup failure is logged but does not send
notifications. This is a small-group setup; verify backup growth and repository
limits as data grows. Losing the recovery key makes the snapshots unreadable.

Before restoring, stop the old poller if it still exists. Build the exact archived
source, restore the SQLite database into `calorie-bot-data:/data/bot_state.db`,
restore configuration as `.env` with mode 600, and use the existing Docker deploy
script. Update provider credentials if revoked and test `/start`, `/today`,
`/test`, user isolation and reminders on the replacement server.
