from __future__ import annotations

import json
from pathlib import Path


STATE_PATH = Path("data/state.json")
PUBLIC_PATH = Path("site/data/listings.json")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen": {}, "listings": []}
    with STATE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def save_public(listings: list[dict], meta: dict, destinations: list[dict]) -> None:
    PUBLIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "destinations": destinations,
        "listings": listings,
    }
    with PUBLIC_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
