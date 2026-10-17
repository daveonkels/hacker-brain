#!/usr/bin/env bash
# Update an already bootstrapped host from any clone. No data overwrites.
set -euo pipefail
host="${HACKER_BRAIN_HOST:?Set HACKER_BRAIN_HOST to your SSH alias}"
ssh "$host" 'bash -se' <<'REMOTE'
set -euo pipefail
cd /opt/hacker-brain
test -z "$(git status --porcelain)" || { echo "Remote checkout has changes; refusing update" >&2; exit 1; }
sudo git fetch origin main
old_revision=$(git rev-parse HEAD)
sudo git merge --ff-only origin/main
python3 -m unittest discover -q
stamp=$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -m 700 /var/backups/hacker-brain
sudo cp /etc/systemd/system/hacker-brain.service "/var/backups/hacker-brain/service.$stamp"
sudo install -m 644 deploy/hacker-brain.service /etc/systemd/system/hacker-brain.service
sudo systemctl daemon-reload
sudo systemctl restart hacker-brain
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:18766/api/stats >/dev/null; then
    printf 'Deployed %s (previous %s)\n' "$(git rev-parse HEAD)" "$old_revision"
    exit 0
  fi
  sleep 2
done
echo "Health check failed; inspect hacker-brain.service. Previous revision: $old_revision" >&2
exit 1
REMOTE
