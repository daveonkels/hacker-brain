#!/usr/bin/env bash
set -euo pipefail
# Broker authentication only; the upstream OpenAI key remains in Agent Vault.
export AGENT_VAULT_TOKEN
AGENT_VAULT_TOKEN="$(cat "${CREDENTIALS_DIRECTORY:?}/agent-vault-token")"
export AGENT_VAULT_ADDR=http://127.0.0.1:14321
export AGENT_VAULT_VAULT=default
export AGENT_VAULT_TELEMETRY=0
exec /usr/local/bin/agent-vault vault run -- \
  /usr/bin/python3 /opt/hacker-brain/search.py \
  --data /var/lib/hacker-brain/sample --serve --port 18766 --reranker decisions
