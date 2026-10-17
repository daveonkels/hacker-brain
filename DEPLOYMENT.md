# Private homelab deployment

This deployment runs independently of a workstation: a Linux systemd service serves the app, a dedicated rootless Docker container runs embeddings, and Agent Vault brokers Decisions requests on the same server. Tailscale Serve provides private HTTPS. There is no public listener, application login, automatic collection, or workstation SSH dependency.

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
| Private URL | `https://<your-server-tailnet-name>:8443/` |

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

## Updating from any clone

Commit and push the desired source first. Then:

```sh
HACKER_BRAIN_HOST=your-ssh-alias ./scripts/deploy.sh
```

The script refuses a dirty remote checkout, pulls `main` with a fast-forward merge, runs offline tests, backs up the installed unit, and restarts only Hacker Brain. It checks the local stats endpoint before reporting success. It does not overwrite the sample or pull a new model image. Deployment configuration/model changes need a separate reviewed Compose update.

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
