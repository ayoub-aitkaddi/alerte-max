#!/usr/bin/env python3
"""Affiche le chat ID des personnes qui ont écrit au bot.

1. Envoie /start à ton bot dans Telegram.
2. TELEGRAM_TOKEN=... python scripts/trouver_chat_id.py
Le script attend jusqu’à 30 s qu’un message arrive.
"""
import json
import os
import urllib.request

token = os.environ["TELEGRAM_TOKEN"]
url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=30"
with urllib.request.urlopen(url, timeout=40) as rep:
    mises_a_jour = json.load(rep)["result"]

chats = {(m["chat"]["id"], m["chat"].get("first_name", ""))
         for u in mises_a_jour if (m := u.get("message"))}
if not chats:
    print("Aucun message reçu : envoie /start au bot puis relance.")
for chat_id, prenom in chats:
    print(f"{prenom} → TELEGRAM_CHAT_ID={chat_id}")
