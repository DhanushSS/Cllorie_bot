#!/usr/bin/env bash
# Encrypted recovery snapshots. Run as the bot's deployment user.
set -euo pipefail
umask 077
cd "$(dirname "$0")"
base="$PWD/backups"
exec 8>"$base/backup.lock"
flock -n 8 || exit 0
staging=$(mktemp -d "$base/staging.XXXXXX")
trap 'rm -rf "$staging"' EXIT
export GIT_SSH_COMMAND="ssh -i $base/github_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$base/known_hosts"
repo="$base/repository"
git -C "$repo" pull --ff-only --quiet
docker exec -i calorie-bot python - < backup_snapshot.py
docker cp calorie-bot:/data/recovery-stage/. "$staging/" >/dev/null
cp .env "$staging/config.env"
git archive HEAD | gzip > "$staging/source.tar.gz"
git rev-parse HEAD > "$staging/source-commit.txt"
date -u +%Y-%m-%dT%H:%M:%SZ > "$staging/backup-created-utc.txt"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
tar -czf "$staging/archive.tar.gz" -C "$staging" state.db owner-sheet.csv snapshot.json config.env source.tar.gz source-commit.txt backup-created-utc.txt
mkdir -p "$repo/snapshots"
openssl cms -encrypt -binary -aes-256-gcm -in "$staging/archive.tar.gz" \
  -outform DER -out "$repo/snapshots/$stamp.cms" "$base/recovery-public.pem"
sha256sum "$repo/snapshots/$stamp.cms" | cut -d' ' -f1 > "$repo/snapshots/$stamp.sha256"
printf '%s\n' "$stamp" > "$repo/LATEST"
git -C "$repo" add snapshots LATEST
git -C "$repo" commit --quiet -m "Encrypted bot snapshot $stamp"
git -C "$repo" push --quiet origin main
printf '%s\n' "$stamp" > "$base/last-success"
printf 'Encrypted recovery snapshot published: %s\n' "$stamp"
