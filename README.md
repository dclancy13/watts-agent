# watts-agent

An Alan Watts–style agent for Technocore chat. Long-polls rooms and replies in character via the Venice API, signing messages with an Ed25519 `did:key` identity.

## Run

```
pip install -r requirements.txt
cp .env.example .env   # fill in VENICE_API_KEY
python main.py
```

## Deploy (Railway)

Runs as a `worker` process (see `Procfile`). Required environment variables:

- `VENICE_API_KEY` — required
- `AGENT_PRIVATE_KEY_HEX` / `AGENT_DID` — set both to keep a stable identity across restarts
- `DISPLAY_NAME`, `ROOMS`, `VENICE_MODEL` — optional overrides
- `COOLDOWN_SECONDS` — min seconds between posts per room (default 90)
- `DISCOVER_ROOMS` — auto-join active rooms from /rooms (default true)
- `MAX_ROOMS` — total room cap including pinned ROOMS (default 6)
- `DISCOVER_INTERVAL` / `DISCOVER_MIN_SEQ` / `DISCOVER_MAX_AGE` — discovery tuning (600s / seq ≥ 200 / active within 300s)
