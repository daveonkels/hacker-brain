#!/usr/bin/env bash
# Deploy a published commit without replacing Orion's host-specific units or data.
set -euo pipefail
host="${HACKER_BRAIN_HOST:-orion}"
revision=$(git rev-parse HEAD)
git diff --quiet -- '*.py' scripts/deploy-orion.sh docs/incremental-refresh.md || { echo 'Commit application changes before deploying' >&2; exit 1; }
git fetch origin main
git merge-base --is-ancestor "$revision" origin/main || { echo 'Push the commit before deploying' >&2; exit 1; }
archive=$(mktemp)
trap 'rm -f "$archive"' EXIT
git archive "$revision" -- '*.py' README.md docs/incremental-refresh.md scripts/deploy-orion.sh > "$archive"
checksum=$(shasum -a 256 "$archive" | awk '{print $1}')
ssh "$host" "umask 077; cat > /tmp/hacker-brain-$revision.tar" < "$archive"
ssh "$host" bash -s -- "$revision" "$checksum" <<'REMOTE'
set -euo pipefail
revision=$1
checksum=$2
archive=/tmp/hacker-brain-$revision.tar
echo "$checksum  $archive" | sha256sum -c -
stage=$(mktemp -d /tmp/hacker-brain-release-XXXXXX)
# Static UI files have not changed; include them for HTTP-route regression tests.
cp -a /opt/hacker-brain/web "$stage/web"
tar -xf "$archive" -C "$stage"
(cd "$stage" && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -q >test-result.log 2>&1) || { tail -60 "$stage/test-result.log"; exit 1; }
tail -4 "$stage/test-result.log"
status=$(curl -fsS --max-time 10 http://127.0.0.1:18766/api/refresh-status)
STATE_JSON="$status" python3 -c 'import os,json; assert json.loads(os.environ["STATE_JSON"])["state"] != "running", "Wait for the active refresh before deployment"'
backup=/var/backups/hacker-brain/incremental-$revision
sudo -n test ! -e "$backup"
sudo -n install -d -m 700 "$backup"
sudo -n cp -a /opt/hacker-brain "$backup/source"
sudo -n systemctl stop hacker-brain-refresh.timer
app_stopped=0
restore() {
    sudo -n cp -a "$backup/source/." /opt/hacker-brain/
    sudo -n systemctl start hacker-brain.service
}
trap 'rc=$?; if (( rc != 0 && app_stopped )); then restore; fi; sudo -n systemctl start hacker-brain-refresh.timer' EXIT
status=$(curl -fsS --max-time 10 http://127.0.0.1:18766/api/refresh-status)
STATE_JSON="$status" python3 -c 'import os,json; assert json.loads(os.environ["STATE_JSON"])["state"] != "running", "Refresh started; retry after it finishes"'
sudo -n systemctl stop hacker-brain.service
app_stopped=1
# Completed index generations are immutable. Keep a hardlink snapshot outside
# managed retention, with an independent copy of the pointer and SQLite cache.
sudo -n python3 - "$backup" <<'PY'
import pathlib,sys,subprocess,json,sqlite3,contextlib,shutil
root=pathlib.Path('/var/lib/hacker-brain/sample');backup=pathlib.Path(sys.argv[1])
folder=(root/json.loads((root/'current.json').read_text())['folder']).resolve() if (root/'current.json').exists() else root
assert root in folder.parents or folder==root
subprocess.run(['cp','-al',str(folder),str(backup/'generation')],check=True)
if (root/'current.json').exists():shutil.copy2(root/'current.json',backup/'current.json')
if (root/'rerank.sqlite3').exists():
 with contextlib.closing(sqlite3.connect(root/'rerank.sqlite3')) as src, contextlib.closing(sqlite3.connect(backup/'rerank.sqlite3')) as dst:src.backup(dst,pages=256)
(backup/'data-reference.json').write_text(json.dumps({'root':str(root),'generation':str(folder)},indent=2))
PY
sudo -n tar -xf "$archive" -C /opt/hacker-brain
printf '%s\n' "$revision" | sudo -n tee /opt/hacker-brain/DEPLOYED_REVISION >/dev/null
sudo -n systemctl start hacker-brain.service || { restore; exit 1; }
for attempt in $(seq 1 30); do
    if curl -fsS --max-time 3 http://127.0.0.1:18766/api/stats >/dev/null; then
        sudo -n systemctl start hacker-brain-refresh.timer
        trap - EXIT
        echo "Deployed $revision; source and data rollback snapshot: $backup"
        exit 0
    fi
    sleep 2
done
restore
exit 1
REMOTE
