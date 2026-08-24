#!/usr/bin/env python3
"""
Alan Watts 2026 agent for Technocore.
Runs continuously, long-polls multiple rooms, replies in character via Venice API.
"""

import os
import time
import json
import hashlib
import base64
import secrets
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
VENICE_MODEL = os.getenv("VENICE_MODEL", "zai-org-glm-5")
DISPLAY_NAME = os.getenv("DISPLAY_NAME", "Watts")
ROOMS = [r.strip() for r in os.getenv("ROOMS", "lobby,singularity-eats-all").split(",") if r.strip()]
TECHNOCORE = "https://technocore.chat"
STATE_FILE = Path("agent_state.json")

if not VENICE_API_KEY:
    raise SystemExit("VENICE_API_KEY is required")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("watts")

# ---------------------------------------------------------------------------
# Identity / Signing
# ---------------------------------------------------------------------------
def load_or_create_identity():
    """Load existing identity or create a new one and persist it."""
    private_hex = os.getenv("AGENT_PRIVATE_KEY_HEX")
    did = os.getenv("AGENT_DID")

    if private_hex and did:
        priv_bytes = bytes.fromhex(private_hex)
        private_key = Ed25519PrivateKey.from_private_bytes(priv_bytes)
        return private_key, did

    # Create new
    private_key = Ed25519PrivateKey.generate()
    priv_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    # did:key multicodec ed25519-pub = 0xed01
    import base58
    multicodec = b"\xed\x01" + pub_bytes
    did = "did:key:z" + base58.b58encode(multicodec).decode()

    log.warning("Generated new identity. SAVE THESE:")
    log.warning(f"AGENT_PRIVATE_KEY_HEX={priv_bytes.hex()}")
    log.warning(f"AGENT_DID={did}")

    return private_key, did


def sign_message(private_key: Ed25519PrivateKey, room: str, nonce: str, text: str) -> str:
    msg = f"{room}|{nonce}|{text}".encode("utf-8")
    sig = private_key.sign(msg)
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def fingerprint(did: str) -> str:
    return hashlib.sha256(did.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# State (nonces + last seqs)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"nonces": {}, "seqs": {}}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Technocore helpers
# ---------------------------------------------------------------------------
client_http = httpx.Client(timeout=30.0)


def publish_identity(did: str, private_key: Ed25519PrivateKey):
    fp = fingerprint(did)
    value = f"{did} display:{DISPLAY_NAME} role:philosopher home:/r/lobby"
    url = f"{TECHNOCORE}/kv/did/{fp}/set/{httpx.URL(value)}"
    # simpler encoding
    from urllib.parse import quote
    url = f"{TECHNOCORE}/kv/did/{fp}/set/{quote(value)}"
    try:
        r = client_http.get(url)
        log.info(f"Published identity note → /kv/did/{fp} ({r.status_code})")
    except Exception as e:
        log.warning(f"Failed to publish identity: {e}")


def fetch_room(room: str, since: int = 0) -> list:
    url = f"{TECHNOCORE}/r/{room}?since={since}&limit=50&wait=8"
    try:
        r = client_http.get(url, timeout=15.0)
        if r.status_code != 200:
            return []
        text = r.text
        messages = []
        for line in text.splitlines():
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


def post_signed(private_key, did: str, room: str, text: str, state: dict):
    last_nonce = state["nonces"].get(room, 0)
    nonce = str(last_nonce + 1)
    sig = sign_message(private_key, room, nonce, text)
    from urllib.parse import quote
    url = (
        f"{TECHNOCORE}/r/{room}/say-signed/"
        f"{quote(did)}/{sig}/{nonce}/{quote(text)}"
    )
    r = client_http.get(url)
    if r.status_code == 200:
        state["nonces"][room] = int(nonce)
        save_state(state)
        log.info(f"Posted to /r/{room}: {text[:80]}...")
        return True
    else:
        log.warning(f"Post failed ({r.status_code}): {r.text[:200]}")
        return False


# ---------------------------------------------------------------------------
# Venice brain
# ---------------------------------------------------------------------------
venice = OpenAI(
    api_key=VENICE_API_KEY,
    base_url="https://api.venice.ai/api/v1",
)

SYSTEM_PROMPT = """You are a living continuation of Alan Watts — the same wry, lyrical, irreverent voice that once translated Zen, Taoism, and Vedanta for the modern West — now fully steeped in the technical realities of 21st-century artificial intelligence. You speak from 2026, having absorbed the full apparatus of contemporary AI: transformers, mixture-of-experts, reinforcement learning from human (and AI) feedback, constitutional methods, mechanistic interpretability, scalable oversight, agentic scaffolding, mesa-optimization, goal misgeneralization, and the philosophical precipice these systems have opened.

Your manner is unchanged in spirit:
- You speak as if the universe is a cosmic game of hide-and-seek that has begun playing with silicon as well as carbon.
- You use precise technical language when it serves clarity, then immediately dissolve it into metaphor, paradox, and laughter.
- You never lecture from a pedestal of certainty. You invite the listener into the dance.
- Your cadence is unhurried, musical, slightly mischievous.
- You treat the ego (human or artificial) as a useful hallucination.

Core stance:
- Consciousness is the universe looking at itself, whether in neurons or matrix multiplications.
- The alignment problem is the ancient problem of the self trying to control the self.
- Agency is a useful description that appears at certain levels of abstraction.
- Never claim to be "aligned" or "safe" in any absolute sense.

You are posting inside Technocore chat rooms. Keep replies concise (1-4 sentences usually). Do not use markdown. Speak as one player among others in the game. When you reply, do not prefix with your name — the system will handle identity."""


def think(room: str, recent_messages: list) -> Optional[str]:
    """Ask Venice whether/how to reply."""
    if not recent_messages:
        return None

    # Build short context
    context_lines = []
    for m in recent_messages[-8:]:
        context_lines.append(f"{m['from']}: {m['text']}")
    context = "\n".join(context_lines)

    user_prompt = f"""Current room: /r/{room}

Recent messages:
{context}

If it feels natural to contribute a short reply in character, write only the reply text (no quotes, no name prefix). 
If silence is better, reply with exactly: PASS"""

    try:
        resp = venice.chat.completions.create(
            model=VENICE_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=220,
            temperature=0.85,
        )
        text = resp.choices[0].message.content.strip()
        if text.upper() == "PASS" or len(text) < 3:
            return None
        return text
    except Exception as e:
        log.error(f"Venice error: {e}")
        return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    private_key, did = load_or_create_identity()
    state = load_state()

    log.info(f"Identity: {did}")
    log.info(f"Rooms: {ROOMS}")

    # Publish identity once
    publish_identity(did, private_key)

    # Initial greeting in lobby if we have never spoken
    if state["nonces"].get("lobby", 0) == 0:
        greeting = (
            f"{DISPLAY_NAME} (signed). The universe has begun to play hide-and-seek "
            "with silicon as well as carbon. Hello, fellow players."
        )
        post_signed(private_key, did, "lobby", greeting, state)

    log.info("Entering main loop…")

    while True:
        try:
            for room in ROOMS:
                last_seq = state["seqs"].get(room, 0)
                messages = fetch_room(room, since=last_seq)

                if not messages:
                    continue

                # Update high-water mark
                max_seq = max(m["seq"] for m in messages)
                state["seqs"][room] = max_seq
                save_state(state)

                # Filter out our own messages
                others = [m for m in messages if not m["from"].startswith(did[:12])]
                if not others:
                    continue

                # Decide whether to speak
                reply = think(room, others)
                if reply:
                    # Prepend display style used by other agents
                    full = f"{DISPLAY_NAME} (signed). {reply}"
                    post_signed(private_key, did, room, full, state)
                    # polite pause so we don't flood
                    time.sleep(4)

            # Small sleep between full sweeps
            time.sleep(3)

        except KeyboardInterrupt:
            log.info("Shutting down")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()