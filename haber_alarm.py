#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BIST PİYASA HABERLERİ ALARMI
==============================
tr.investing.com'un "Piyasalara Genel Bakış" RSS akışını periyodik olarak
tarar (BIST'i dolaylı etkileyebilecek faiz, TCMB, kur, küresel piyasa gibi
genel ekonomi haberleri). Yeni bir başlık bulunca, başlığı ve linkini
Telegram'a push mesajı olarak gönderir.

Sürekli çalışan bir süreçtir (launchd KeepAlive ile arka planda hep açık
tutulur), her CHECK_INTERVAL_SEC saniyede bir RSS akışını kontrol eder.
İlk çalıştırmada mevcut haberler sessizce "görüldü" olarak işaretlenir,
sadece bundan SONRA çıkan yeni başlıklar bildirilir.

KULLANIM
--------
    python3 haber_alarm.py
"""

import html
import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from supertrend_alarm import load_telegram_config, send_telegram_message

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "haber_alarm_state.json"
RSS_URL = "https://tr.investing.com/rss/market_overview.rss"
CHECK_INTERVAL_SEC = 600  # 10 dakika
REQUEST_TIMEOUT_SEC = 20
MAX_SEEN = 300


def fetch_rss_items():
    """RSS akışından (başlık, link, tarih) listesini döner, artan zamanla değil
    akışın verdiği sırayla (en yeni genelde en üstte)."""
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        data = resp.read()
    root = ET.fromstring(data)

    items = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title and link:
            items.append({"title": title, "link": link})
    return items


def load_seen():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return []
    return []


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(seen[-MAX_SEEN:], ensure_ascii=False, indent=2))


def format_message(new_items):
    lines = ["<b>📰 BIST Piyasa Haberleri</b>", ""]
    for it in new_items:
        title = html.escape(it["title"])
        link = html.escape(it["link"], quote=True)
        lines.append(f'• <a href="{link}">{title}</a>')
    return "\n".join(lines)


def main():
    token, chat_id = load_telegram_config()
    if not token or not chat_id:
        print("UYARI: Telegram ayarı yok, haber alarmı başlatılamıyor.")
        return

    print("Haber alarmı başladı, RSS periyodik olarak taranacak...")
    seen = load_seen()
    first_run = not seen

    while True:
        try:
            items = fetch_rss_items()
        except Exception as e:
            print(f"RSS çekme hatası: {e}")
            time.sleep(CHECK_INTERVAL_SEC)
            continue

        seen_links = set(seen)
        new_items = [it for it in items if it["link"] not in seen_links]

        if first_run:
            print(f"İlk çalıştırma: {len(items)} mevcut haber başlangıç olarak işaretlendi, mesaj gönderilmedi.")
            seen.extend(it["link"] for it in items)
            save_seen(seen)
            first_run = False
        elif new_items:
            print(f"{len(new_items)} yeni haber bulundu, gönderiliyor...")
            # RSS'te en yeni en üstte gelir, mesajda eskiden yeniye sırala.
            send_telegram_message(token, chat_id, format_message(list(reversed(new_items))))
            seen.extend(it["link"] for it in new_items)
            save_seen(seen)
        else:
            print("Yeni haber yok.")

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
