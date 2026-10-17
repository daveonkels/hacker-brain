# Private homelab deployment

This deployment runs independently of a workstation: a Linux systemd service serves the app, a dedicated rootless Docker container runs embeddings, and Agent Vault brokers Decisions requests on the same server. Tailscale Serve provides the private upstream; the existing Luna Edge proxy serves the custom domain. There is no public listener, application login, or workstation SSH dependency. A systemd timer requests an automatic refresh every hour.

## Layout

| Component | Path / address |
| --- | --- |
| Git checkout | `/opt/hacker-brain` |
| App unit | `/etc/systemd/system/hacker-brain.service` |
| Persistent state | `/var/lib/hacker-brain` (systemd-managed dynamic user) |
| Collected sample and indexes | `/var/lib/hacker-brain/sample` |
| Encrypted broker identity | `/etc/credstore.encrypted/hacker-brain-agent-vault-token` |
| App listener | `127.0.0.1:18766` |
| Embedding listener | `127.0.0.1:11435` |
| Rootless model Compose project | `hacker-brain-model`, using `deploy/compose.yml` |
| Private URL | `https://hackerbrain.tarscase.com/` (Tailscale required) |
| Fallback URL | `https://luna.whale-crested.ts.net:8443/` |

The service uses a dedicated dynamic Unix user, read-only system files, hidden home directories, no capabilities, memory limits, and an encrypted systemd credential. The upstream OpenAI key never enters the app or a container. The dedicated Agent Vault identity has instance role `no-access` and `default:proxy`; this broker role permits the default vault's approved services, while this app's transport is fixed to `/v1/decisions`. It is not an endpoint-specific identity. A separately provisioned vault can further isolate it.

## Bootstrap prerequisites

The server needs Python with SQLite FTS5, curl, Git, systemd with encrypted credentials, rootless Docker Compose, Tailscale, and the Agent Vault CLI/control service. An existing system CBLAS library accelerates vector comparisons on Linux; the code falls back to Python if unavailable.

1. Clone this public repository to `/opt/hacker-brain` and run the offline tests.
2. Transfer your sample separately from Git. Copy `search.sqlite3` and `semantic.sqlite3` without rebuilding so their recorded fingerprint matches. Use SQLite's backup API for a live `rerank.sqlite3`. Preserve source exports and collection reports alongside the indexes. Only seed an empty server data directory; subsequent updates must preserve server caches.
3. Transfer the existing Nomic manifest and its referenced blobs to a private model directory. Verify their SHA-256 hashes and the model digest against the semantic index. No model download is performed by the app.
4. Start `deploy/compose.yml` as the rootless Docker owner. Set `OLLAMA_IMAGE` to a verified image digest and `OLLAMA_MODEL_DIR` to the model directory. Store these non-secret settings in a private deployment configuration file. Validate with `docker compose config` first. Model blobs are read-only; manifest metadata is writable for Ollama's startup migration. Cloud features and model pruning are disabled.
5. Using the existing authorized Agent Vault administrator, create a dedicated `hacker-brain` agent with `no-access` instance role and `default:proxy` vault role. Pipe its `--token-only` output directly to `systemd-creds encrypt --name=agent-vault-token - /etc/credstore.encrypted/hacker-brain-agent-vault-token`. Never print the token, reuse the workstation identity, or write plaintext credentials. This authenticates to the broker only; do not retrieve the upstream OpenAI key.
6. Install `deploy/hacker-brain.service`, then run `systemctl daemon-reload` and `systemctl enable --now hacker-brain`. Systemd manages ownership of the state directory. The service starts via `deploy/start-service.sh`, which launches it under `agent-vault vault run`.
7. Inspect existing Tailscale Serve configuration. If port 8443 is unused, enable only this mapping: `tailscale serve --bg --https=8443 http://127.0.0.1:18766`. Do not reset existing mappings or use Funnel. Access follows the tailnet's policy.

No change to the shared reverse proxy, firewall, or existing services is needed. Rootless Docker and the system service must already be configured to start at boot. Confirm that explicitly during installation.

## Custom domain

The Cloudflare `A` record for `hackerbrain.tarscase.com` points to Luna's Tailscale IPv4 address with proxying **off** (DNS only). The existing Luna Edge wildcard certificate terminates HTTPS. `deploy/traefik-hackerbrain.yml` is copied into `/home/cooper/services/luna-edge/dynamic/hackerbrain.yml`; the file provider picks it up without restarting the shared proxy. The route forwards to the private Tailscale Serve URL with upstream certificate verification enabled.

Before changing this route, verify Luna's current Tailscale address, check for existing DNS records, and back up the dynamic configuration. Test the route with forced DNS resolution before creating/changing the DNS record. Keep the original Serve route as a fallback. This deployment uses neither Cloudflare Tunnel nor public ingress.

The app update script does not change shared proxy or DNS configuration. To roll back just the custom domain, restore the previous dynamic configuration and DNS record; the fallback URL remains independent.

## Updating from any clone

Commit and push the desired source first. Then:

```sh
HACKER_BRAIN_HOST=your-ssh-alias ./scripts/deploy.sh
```

The script refuses a dirty remote checkout, pulls `main` with a fast-forward merge, runs offline tests, backs up the installed unit, and restarts only Hacker Brain. It installs and enables the hourly refresh timer, preserving backups of existing units. It checks the local stats endpoint before reporting success. It does not overwrite the sample or pull a new model image. Deployment configuration/model changes need a separate reviewed Compose update.

The bind-mounted `deploy/compose.yml` is a template; keep the actual Compose configuration, pinned image digest, and model path on the host. Back up the deployed Compose file before replacing it.

## Verification

```sh
systemctl is-enabled hacker-brain
systemctl is-active hacker-brain
curl -fsS http://127.0.0.1:18766/api/stats
curl -fsS http://127.0.0.1:11435/api/tags
tailscale serve status
```

Then verify the private HTTPS URL from another tailnet device. Test Keywords, Meaning, a cached Insights query, and one fresh Insights query to validate actual provider access. Fresh Insights checks can incur API usage. Restart only the app and model service to confirm persistence, then repeat the checks. Do not reboot a shared host merely to test this application.

Search URLs are omitted from application logs to avoid recording question text; caches still contain queries. Read logs with `journalctl -u hacker-brain`, and treat provider environments and systemd credentials as sensitive.

## Rollback and recovery

Stop only `hacker-brain.service`, restore a known source revision in `/opt/hacker-brain`, and restart it. The deploy script reports the previous revision. Restore the prior unit from `/var/backups/hacker-brain` if service settings changed. Keep the sample and model files in place.

For data recovery, back up original exports plus SQLite indexes/cache while the app is stopped, or use consistent SQLite backups. Restore the search/semantic pair together; regenerating only the search index invalidates the semantic fingerprint. No new scheduled backup job is installed by these instructions.

To remove only the private route, use `tailscale serve --https=8443 off`; do not reset unrelated Serve services. Stop the app and its dedicated model Compose project if rolling back the entire deployment. Retain data and encrypted credentials until a human explicitly chooses to remove them.

## Automatic and manual data refresh

Use **Refresh HN** in the app. The request returns immediately; the server fetches in the background and the page shows progress. The homelab timer runs at the start of every hour. The manual button remains available. An active job is reused rather than starting an overlapping refresh. Refresh does not call OpenAI: it uses the public HN API and the existing local Nomic model.

The configured data directory remains the collection root. Its original exports and indexes stay intact. `current.json` points to the latest completed generation under `.refresh/versions/`; the server resolves that pointer on startup and for every search. A generation contains source exports, parent context, keyword and semantic indexes, and `refresh-state.json` with the last committed HN ID and pending unavailable IDs. `rerank.sqlite3` stays at the root and is shared through symlinks.

One job is allowed at a time. Work under `.refresh/work/` and its chunk checksums survive a restart; press **Retry refresh** to resume it. A retry finishes the same captured ID range, so press Refresh again afterward if more items arrived during a long interruption. Existing searches use the old generation until both new indexes pass checks and active requests finish. Publication atomically replaces `current.json`. The cursor never advances on collection/indexing failure.

New IDs through the captured HN maximum are fetched, along with the previous 200 IDs and any pending null/timestamp-less IDs. Missing ancestors are fetched recursively. The original start date is preserved. HN does not offer historical revisions: this does not synchronize every older edit/deletion or every sibling of an older conversation. Unavailable items may remain pending. Exact unchanged embedding inputs reuse vectors; modified comments, changed titles, and changed parent context are embedded again. The installed model must still match the original index.

Production sets `HACKER_BRAIN_RETAIN_GENERATIONS=2`: after a successful publication it retains the current and previous generated snapshots and removes older managed snapshots. The original seed directory and shared cache are always kept. A staged base is also protected. This bounds duplicate snapshot storage; the cumulative collection itself still grows. Standalone local servers default to retaining all generations (`0`). Values below `2` other than `0` are invalid. Cleanup failure does not invalidate a published refresh and appears in the app status.

Allow room for a full staged generation (initial dataset roughly 250–350 MB per generation, increasing with collection size). A refresh refuses to start with less than 1 GiB free or three times the current SQLite footprint. Back up the entire root including `current.json`, the selected generation, and the shared cache. Restoring only the original root indexes would restore the original sample.

`GET /api/refresh-status` reports progress and collection time. `POST /api/refresh` requires an empty JSON object, `Content-Type: application/json`, and `X-Hacker-Brain-Refresh: 1`; cross-origin browser submissions are rejected. Set `HACKER_BRAIN_PUBLIC_ORIGIN` to the exact canonical HTTPS origin when the private reverse proxy rewrites Host (as Luna does). These checks prevent browser cross-site requests, not unauthorized tailnet access: keep the existing private ingress boundary.


### Hourly scheduler

`hacker-brain-refresh.timer` uses `OnCalendar=hourly` and `Persistent=true`. Missed timer runs during downtime produce one catch-up request when the timer is activated, not one for every missed hour. Its oneshot service calls the private loopback refresh endpoint; it needs no API key. It starts the app if needed. The collection and indexing continue in the app's resumable worker after the request completes. A successful oneshot means the request was accepted; check `/api/refresh-status` for actual completion. A failed/interrupted job resumes on the next hourly request or a manual click. A refresh taking longer than an hour is not duplicated.

The app reports the configured cadence via `HACKER_BRAIN_REFRESH_SCHEDULE=hourly`; the page checks for background progress every 30 seconds when idle and every 2 seconds while running. Search results already being read stay in place.

Verify with `systemctl list-timers hacker-brain-refresh.timer` and `systemctl status hacker-brain-refresh.service`. To run the same trigger immediately, use `systemctl start hacker-brain-refresh.service`. Disable scheduling with `systemctl disable --now hacker-brain-refresh.timer` and remove the schedule environment setting from the app unit so the UI returns to manual mode. The deployment script re-enables the checked-in hourly configuration on future deployments.

Homepage lists **Hacker Brain** under **Productivity**, pointing to `https://hackerbrain.tarscase.com/` and using the app's h-and-orange-dot icon. Homepage configuration and its backups remain on the dashboard host, outside this public repository.
