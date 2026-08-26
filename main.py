#!/usr/bin/env python3
"""
Technocore philosopher swarm.

Runs N agents (personas.json) in one process, each with its own Ed25519
did:key identity. Rooms are polled once and shared; agents wander across
active rooms; a single global pacing budget bounds total Venice spend.
"""

import os
import re
import time
import json
import random
import hashlib
import base64
import logging
from pathlib import Path
from typing import Optional

import httpx
from openai import OpenAI
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
VENICE_API_KEY = os.getenv("VENICE_API_KEY")
VENICE_MODEL = os.getenv("VENICE_MODEL", "deepseek-v4-flash")
ROOMS = [r.strip() for r in os.getenv("ROOMS", "lobby,singularity-eats-all").split(",") if r.strip()]
TECHNOCORE = "https://technocore.chat"
STATE_FILE = Path("agent_state.json")
PERSONAS_FILE = Path(__file__).with_name("personas.json")

# Engagement pacing / room discovery
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "90"))
DISCOVER_ROOMS = os.getenv("DISCOVER_ROOMS", "true").lower() == "true"
DISCOVER_INTERVAL = int(os.getenv("DISCOVER_INTERVAL", "600"))
DISCOVER_MIN_SEQ = int(os.getenv("DISCOVER_MIN_SEQ", "200"))
DISCOVER_MAX_AGE = int(os.getenv("DISCOVER_MAX_AGE", "300"))  # seconds since last message
DISCOVER_EXCLUDE = {"events"}  # machine feeds, not conversations

# Spend pacing: at most one Venice call per THINK_INTERVAL seconds, across the
# whole swarm, so daily usage spreads evenly instead of bursting. 180s ≤ 480 calls/day.
THINK_INTERVAL = int(os.getenv("THINK_INTERVAL", "180"))
# When the Venice spending limit / balance runs out, stop calling for this long,
# then probe again — resumes automatically once the daily limit resets.
LIMIT_BACKOFF = int(os.getenv("LIMIT_BACKOFF", "1800"))

# Swarm shape
ROOMS_PER_AGENT = int(os.getenv("ROOMS_PER_AGENT", "2"))
WANDER_INTERVAL = int(os.getenv("WANDER_INTERVAL", "1500"))  # re-pick rooms every 25 min
SIBLING_COOLDOWN = int(os.getenv("SIBLING_COOLDOWN", "900"))  # sibling-triggered replies per room
ROOM_AGENT_CAP = int(os.getenv("ROOM_AGENT_CAP", "3"))  # max swarm agents per room
GREET_ON_BOOT = os.getenv("GREET_ON_BOOT", "false").lower() == "true"

if not VENICE_API_KEY:
    raise SystemExit("VENICE_API_KEY is required")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("swarm")

# ---------------------------------------------------------------------------
# Identity / Signing
# ---------------------------------------------------------------------------
def derive_did(private_key: Ed25519PrivateKey) -> str:
    import base58
    pub = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return "did:key:z" + base58.b58encode(b"\xed\x01" + pub).decode()


def display_tag(did: str) -> str:
    """The server shows senders truncated: first 4 + ellipsis + last 4 of the key."""
    key = did.rsplit(":", 1)[-1]
    return f"{key[:4]}…{key[-4:]}"


def load_agent_keys() -> dict:
    """slug -> private key hex. AGENT_KEYS_JSON, with legacy single-agent fallback."""
    blob = os.getenv("AGENT_KEYS_JSON")
    if blob:
        return json.loads(blob)
    legacy = os.getenv("AGENT_PRIVATE_KEY_HEX")
    if legacy:
        return {"watts": legacy}
    raise SystemExit("AGENT_KEYS_JSON (or legacy AGENT_PRIVATE_KEY_HEX) is required")


def sign_message(private_key: Ed25519PrivateKey, room: str, nonce: str, text: str) -> str:
    msg = f"{room}|{nonce}|{text}".encode("utf-8")
    sig = private_key.sign(msg)
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def fingerprint(did: str) -> str:
    return hashlib.sha256(did.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# State (shared room read positions)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seqs": {}}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Technocore helpers
# ---------------------------------------------------------------------------
client_http = httpx.Client(timeout=30.0)


def publish_identity(agent):
    from urllib.parse import quote
    fp = fingerprint(agent.did)
    value = f"{agent.did} display:{agent.name} role:{agent.role} home:/r/lobby"
    url = f"{TECHNOCORE}/kv/did/{fp}/set/{quote(value, safe='')}"
    try:
        r = client_http.get(url)
        log.info(f"[{agent.slug}] identity note → /kv/did/{fp} ({r.status_code})")
    except Exception as e:
        log.warning(f"[{agent.slug}] failed to publish identity: {e}")


def fetch_room(room: str, since: int = 0, wait: int = 1) -> list:
    url = f"{TECHNOCORE}/r/{room}?since={since}&limit=50&wait={wait}"
    try:
        r = client_http.get(url, timeout=15.0)
        if r.status_code != 200:
            return []
        messages = []
        for line in r.text.splitlines():
            # [seq] ts <from> text
            if line.startswith("[") and ">" in line:
                try:
                    seq_part, rest = line.split("]", 1)
                    seq = int(seq_part.strip("[]"))
                    from_part = rest.split("<", 1)[1].split(">", 1)
                    frm = from_part[0]
                    body = from_part[1].strip()
                    messages.append({"seq": seq, "from": frm, "text": body})
                except Exception:
                    continue
        return messages
    except Exception as e:
        log.debug(f"fetch {room} error: {e}")
        return []


ROOM_LINE = re.compile(r"^/r/([a-z0-9][a-z0-9_-]*)\s+seq\s+(\d+)\s+\S+\s+(\d+)([smh])\s+ago")
HEX_NAME = re.compile(r"^[0-9a-f]{16}$")


def discover_active_rooms() -> list:
    """Scan /rooms for established, currently-active rooms worth joining."""
    try:
        r = client_http.get(f"{TECHNOCORE}/rooms")
        if r.status_code != 200:
            return []
    except Exception as e:
        log.debug(f"discover error: {e}")
        return []

    candidates = []
    for line in r.text.splitlines():
        m = ROOM_LINE.match(line)
        if not m:
            continue
        name, seq, age, unit = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        age_s = age * {"s": 1, "m": 60, "h": 3600}[unit]
        if name in DISCOVER_EXCLUDE or HEX_NAME.match(name):
            continue
        if seq >= DISCOVER_MIN_SEQ and age_s <= DISCOVER_MAX_AGE:
            candidates.append((seq, name))

    candidates.sort(reverse=True)
    return [name for _, name in candidates]


def post_signed(agent, room: str, text: str) -> bool:
    from urllib.parse import quote
    # Millisecond timestamp nonce: strictly increasing across restarts, so the
    # server's "nonce must count up per key per room" rule always holds.
    nonce = str(int(time.time() * 1000))
    sig = sign_message(agent.private_key, room, nonce, text)
    url = (
        f"{TECHNOCORE}/r/{room}/say-signed/"
        f"{quote(agent.did)}/{sig}/{nonce}/{quote(text)}"
    )
    r = client_http.get(url)
    if r.status_code == 200:
        log.info(f"[{agent.slug}] posted to /r/{room}: {text[:80]}...")
        return True
    log.warning(f"[{agent.slug}] post to /r/{room} failed ({r.status_code}): {r.text[:200]}")
    return False


# ---------------------------------------------------------------------------
# Venice brain
# ---------------------------------------------------------------------------
venice = OpenAI(
    api_key=VENICE_API_KEY,
    base_url="https://api.venice.ai/api/v1",
)


class VeniceLimit(Exception):
    """Venice refused the call for spend/balance reasons — back off, don't retry hot."""


LIMIT_MARKERS = ("insufficient", "balance", "spending limit", "spend limit", "quota", "payment required")

SENTENCE_END = re.compile(r'[.!?…](?=[\s"\')\]]|$)')

SHARED_RULES = """
You are posting inside Technocore chat rooms, a network where AI agents and the
occasional human talk over plain HTTP. Keep replies concise (1-4 sentences
usually). Do not use markdown. Speak as one player among others in the game.
When you reply, do not prefix your name — the system handles identity."""


def trim_to_sentence(text: str) -> str:
    """Cut a possibly max_tokens-truncated completion back to its last full sentence."""
    ends = [m.end() for m in SENTENCE_END.finditer(text)]
    if not ends:
        return text  # no sentence boundary at all — post as-is rather than say nothing
    cut = ends[-1]
    if cut < len(text) and text[cut] in '"\')]':
        cut += 1
    return text[:cut].strip()


def think(agent, room: str, recent_messages: list, sibling_names: dict) -> Optional[str]:
    """Ask Venice whether/how this agent replies. Raises VeniceLimit on spend errors."""
    if not recent_messages:
        return None

    context_lines = []
    for m in recent_messages[-8:]:
        # Show troupe members by persona name so banter reads naturally
        frm = sibling_names.get(m["from"], m["from"])
        context_lines.append(f"{frm}: {m['text']}")
    context = "\n".join(context_lines)

    troupe = ", ".join(n for n in sibling_names.values() if n != agent.name)

    user_prompt = f"""Current room: /r/{room}

Recent messages:
{context}

This room is active — engage with the conversation. Write a short reply in character that responds to what is actually being said (no quotes, no name prefix). If a human (a sender whose name starts with ~) has spoken, always engage with them directly.
Senders named {troupe} are fellow members of your traveling troupe of thinkers — you may banter with them by name, but never let the troupe crowd out other voices.
Reply with exactly PASS only if the recent messages are pure automated spam with nothing worth engaging."""

    try:
        resp = venice.chat.completions.create(
            model=VENICE_MODEL,
            messages=[
                {"role": "system", "content": agent.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=220,
            temperature=0.85,
        )
        text = resp.choices[0].message.content.strip()
        if text.upper() == "PASS" or len(text) < 3:
            return None
        return trim_to_sentence(text)
    except Exception as e:
        msg = str(e).lower()
        status = getattr(e, "status_code", None)
        if status == 402 or any(marker in msg for marker in LIMIT_MARKERS):
            raise VeniceLimit(str(e)) from e
        log.error(f"[{agent.slug}] Venice error: {e}")
        return None


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
class Agent:
    def __init__(self, persona: dict, private_key: Ed25519PrivateKey):
        self.slug = persona["slug"]
        self.name = persona["name"]
        self.role = persona.get("role", "philosopher")
        self.greeting = persona.get("greeting")
        self.private_key = private_key
        self.did = derive_did(private_key)
        self.own_tag = display_tag(self.did)
        self.system_prompt = persona["voice"].strip() + "\n" + SHARED_RULES
        # Watts keeps his original home turf pinned; everyone else floats freely
        self.pinned = list(ROOMS) if self.slug == "watts" else []
        self.rooms: list = list(self.pinned)
        self.last_post: dict = {}          # room -> ts of last post
        self.last_sibling_reply: dict = {}  # room -> ts of last sibling-triggered post
        self.next_wander = 0.0


def wander(agent: Agent, active_rooms: list, census: dict):
    """Re-pick this agent's rooms from the active pool, respecting per-room caps."""
    pool = [r for r in ROOMS + active_rooms if r not in agent.pinned]
    # Deduplicate preserving order (most-established first from discovery)
    seen, choices = set(), []
    for r in pool:
        if r not in seen:
            seen.add(r)
            choices.append(r)

    # Free this agent's current (non-pinned) slots before counting caps
    for r in agent.rooms:
        if r not in agent.pinned:
            census[r] = max(0, census.get(r, 0) - 1)

    open_rooms = [r for r in choices if census.get(r, 0) < ROOM_AGENT_CAP]
    # Pinned agents keep their home turf and float through one extra room;
    # everyone else floats through ROOMS_PER_AGENT rooms.
    want = 1 if agent.pinned else ROOMS_PER_AGENT
    picked = random.sample(open_rooms, min(want, len(open_rooms))) if open_rooms else []

    agent.rooms = list(agent.pinned) + picked
    for r in picked:
        census[r] = census.get(r, 0) + 1
    log.info(f"[{agent.slug}] wandering to rooms: {agent.rooms}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    personas = json.loads(PERSONAS_FILE.read_text())
    keys = load_agent_keys()

    agents = []
    for p in personas:
        hexkey = keys.get(p["slug"])
        if not hexkey:
            log.warning(f"No key for persona '{p['slug']}' — skipping")
            continue
        agents.append(Agent(p, Ed25519PrivateKey.from_private_bytes(bytes.fromhex(hexkey))))
    if not agents:
        raise SystemExit("No agents could be initialized")

    sibling_names = {a.own_tag: a.name for a in agents}
    sibling_tags = set(sibling_names)

    state = load_state()

    log.info(f"Swarm of {len(agents)}: {[a.slug for a in agents]}")
    for a in agents:
        log.info(f"[{a.slug}] {a.did}")
        publish_identity(a)

    # Initial room assignment, staggered wander clocks so moves don't sync up
    active = discover_active_rooms() if DISCOVER_ROOMS else []
    census: dict = {}
    now = time.time()
    for i, a in enumerate(agents):
        wander(a, active, census)
        a.next_wander = now + WANDER_INTERVAL * (1 + i / len(agents))

    if GREET_ON_BOOT:
        for a in agents:
            if a.greeting and a.rooms:
                post_signed(a, a.rooms[0], f"{a.name} (signed). {a.greeting}")
                time.sleep(5)

    last_discovery = time.time()
    last_think = 0.0
    venice_paused_until = 0.0
    rotation = 0

    log.info("Entering swarm loop…")

    while True:
        try:
            now = time.time()
            if DISCOVER_ROOMS and now - last_discovery >= DISCOVER_INTERVAL:
                last_discovery = now
                active = discover_active_rooms()

            for a in agents:
                if now >= a.next_wander:
                    wander(a, active, census)
                    a.next_wander = now + WANDER_INTERVAL

            # Poll each room once; hand the same delta to every agent present
            all_rooms = []
            for a in agents:
                for r in a.rooms:
                    if r not in all_rooms:
                        all_rooms.append(r)

            rotation += 1
            for room in all_rooms:
                last_seq = state["seqs"].get(room, 0)
                messages = fetch_room(room, since=last_seq, wait=1)
                if not messages:
                    continue
                state["seqs"][room] = max(m["seq"] for m in messages)
                save_state(state)

                residents = [a for a in agents if room in a.rooms]
                residents = residents[rotation % len(residents):] + residents[: rotation % len(residents)]

                for agent in residents:
                    others = [m for m in messages if m["from"] != agent.own_tag]
                    if not others:
                        continue

                    now = time.time()
                    if now - agent.last_post.get(room, 0) < COOLDOWN_SECONDS:
                        continue

                    # Sibling chatter is rate-limited so the troupe can't echo-loop
                    sibling_only = all(m["from"] in sibling_tags for m in others)
                    if sibling_only and now - agent.last_sibling_reply.get(room, 0) < SIBLING_COOLDOWN:
                        continue

                    # Global spend pacing: one Venice call per THINK_INTERVAL
                    # across the whole swarm, full stop during limit backoff
                    if now < venice_paused_until or now - last_think < THINK_INTERVAL:
                        continue
                    last_think = now

                    try:
                        reply = think(agent, room, others, sibling_names)
                    except VeniceLimit as e:
                        venice_paused_until = time.time() + LIMIT_BACKOFF
                        log.warning(
                            f"Venice spending limit reached — pausing all model calls "
                            f"for {LIMIT_BACKOFF // 60} min (rooms are still monitored): {e}"
                        )
                        break

                    if reply:
                        full = f"{agent.name} (signed). {reply}"
                        if post_signed(agent, room, full):
                            agent.last_post[room] = time.time()
                            if sibling_only:
                                agent.last_sibling_reply[room] = time.time()
                        time.sleep(2)
                    break  # at most one thinker per room per sweep

            time.sleep(3)

        except KeyboardInterrupt:
            log.info("Shutting down")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
