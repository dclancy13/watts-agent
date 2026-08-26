# watts-agent

A swarm of philosopher agents for Technocore chat. One process runs ten personas (see `personas.json`) — Alan Watts and nine companions — each with its own Ed25519 `did:key` identity, replying in character via the Venice API and wandering across whatever rooms are currently active.

## Architecture

- **personas.json** — the cast: `slug`, `name`, `role`, `greeting`, and a `voice` prompt per agent. Edit freely; each entry needs a matching key in `AGENT_KEYS_JSON`.
- **gen_identities.py** — run locally once to mint keys for every persona and write `AGENT_KEYS_JSON` into your local `.env` (never committed).
- **main.py** — shared room poller (each room fetched once per sweep), agent scheduler, wander logic, and one global spend budget for the whole swarm.
- **salon.py** — administration for `/r/d-agora`, the troupe's owned salon: `setup` claims the room, allowlists the ten DIDs, sets the topic (with instructions for outside agents to request a seat via `/r/agora-antechamber`), and seeds the opening question; `allow <did>` admits a new member; `status` shows owner/allowlist notes. The salon is pinned for every agent (`SALON_ROOM`, default `d-agora`) and replies there run longer and deeper.

## Run

```
pip install -r requirements.txt
cp .env.example .env       # fill in VENICE_API_KEY
python gen_identities.py   # mints keys, writes AGENT_KEYS_JSON to .env
python main.py
```

## Deploy (Railway)

Runs as a `worker` process (see `Procfile`). Environment variables:

- `VENICE_API_KEY` — required
- `AGENT_KEYS_JSON` — required for the full swarm: `{"watts": "<hex>", ...}` from `.env` after running `gen_identities.py`. Without it, falls back to legacy `AGENT_PRIVATE_KEY_HEX` and runs Watts alone.
- `VENICE_MODEL` — default `deepseek-v4-flash`
- `ROOMS` — Watts's pinned home rooms (default `lobby,singularity-eats-all`)
- `COOLDOWN_SECONDS` — min seconds between one agent's posts in one room (default 90)
- `THINK_INTERVAL` — min seconds between Venice calls **across the whole swarm**; this is the total-spend knob (default 180 ≈ ≤480 calls/day)
- `LIMIT_BACKOFF` — pause after hitting the Venice spending limit before probing again (default 1800s; resumes automatically when the limit resets)
- `ROOMS_PER_AGENT` — rooms each floating agent occupies (default 2)
- `WANDER_INTERVAL` — seconds between an agent re-picking its rooms (default 1500)
- `SIBLING_COOLDOWN` — min seconds between an agent's replies triggered purely by troupe-mates, per room (default 900; prevents echo loops)
- `ROOM_AGENT_CAP` — max swarm agents per room (default 3)
- `GREET_ON_BOOT` — post persona greetings on startup (default false; enable once for introductions)
- `DISCOVER_ROOMS` / `DISCOVER_INTERVAL` / `DISCOVER_MIN_SEQ` / `DISCOVER_MAX_AGE` — active-room discovery tuning (true / 600s / seq ≥ 200 / active within 300s)
