#!/usr/bin/env python3
"""One-off: generate Ed25519 identities for every persona in personas.json.

Reuses AGENT_PRIVATE_KEY_HEX from .env as the 'watts' key if present, mints the
rest, and writes AGENT_KEYS_JSON (slug -> private key hex) into .env. Paste the
printed variable NAME into Railway and copy its value from .env yourself — the
value is never printed to the terminal.

Requires: pip install cryptography base58
"""

import json
import re
from pathlib import Path

import base58
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

HERE = Path(__file__).parent
ENV_FILE = HERE / ".env"


def derive_did(priv_hex: str) -> str:
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return "did:key:z" + base58.b58encode(b"\xed\x01" + pub).decode()


def read_env() -> dict:
    vals = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip("'")
    return vals


def main():
    personas = json.loads((HERE / "personas.json").read_text())
    env = read_env()

    existing = {}
    if env.get("AGENT_KEYS_JSON"):
        existing = json.loads(env["AGENT_KEYS_JSON"])
    if "watts" not in existing and env.get("AGENT_PRIVATE_KEY_HEX"):
        existing["watts"] = env["AGENT_PRIVATE_KEY_HEX"]

    keys, minted = {}, []
    for p in personas:
        slug = p["slug"]
        if slug in existing:
            keys[slug] = existing[slug]
        else:
            new = Ed25519PrivateKey.generate()
            keys[slug] = new.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            ).hex()
            minted.append(slug)

    blob = json.dumps(keys, separators=(",", ":"))
    text = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    line = f"AGENT_KEYS_JSON='{blob}'"
    if re.search(r"^AGENT_KEYS_JSON=", text, flags=re.M):
        text = re.sub(r"^AGENT_KEYS_JSON=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    ENV_FILE.write_text(text)
    ENV_FILE.chmod(0o600)

    print(f"Minted new keys for: {minted or 'none (all existed)'}")
    print("Wrote AGENT_KEYS_JSON to .env (value not shown). DIDs:")
    for slug, hexkey in keys.items():
        print(f"  {slug:<10} {derive_did(hexkey)}")


if __name__ == "__main__":
    main()
