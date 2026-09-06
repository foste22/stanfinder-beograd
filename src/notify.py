from __future__ import annotations

import os
import requests


def notify_telegram(listing: dict) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False

    approx = " (približna lokacija)" if listing.get("approximate_location") else ""
    d1, d2 = listing["destination_averages"]

    text = (
        f"🏠 NOVI STAN KOJI PROLAZI FILTER\n\n"
        f"💶 {listing['price_eur']} €\n"
        f"📍 {listing['address']}{approx}\n"
        f"📐 {listing.get('area_m2') or '?'} m²\n"
        f"🚍 Prosek: {listing['average_minutes']} min\n"
        f"🎓 Fakultet 1: {d1} min\n"
        f"🎓 Fakultet 2: {d2} min\n\n"
        f"🔗 {listing['url']}"
    )

    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    r.raise_for_status()
    return True
