#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BIST PİYASA HABERLERİ ALARMI
==============================
Foreks'in ekonomi RSS akışını periyodik olarak tarar (BIST'i doğrudan ya
da dolaylı etkileyebilecek şirket, faiz, TCMB, kur, küresel piyasa gibi
haberler). Yeni bir haber bulunca, başlığını ve kısa içerik özetini
Telegram'a AYRI birer push mesajı olarak gönderir.

Sürekli çalışan bir süreçtir (launchd KeepAlive ile arka planda hep açık
tutulur), her CHECK_INTERVAL_SEC saniyede bir RSS akışını kontrol eder.
İlk çalıştırmada mevcut haberler sessizce "görüldü" olarak işaretlenir,
sadece bundan SONRA çıkan yeni haberler bildirilir.

KULLANIM
--------
    python3 haber_alarm.py
"""

import html
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from supertrend_alarm import load_telegram_config, send_telegram_message

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "haber_alarm_state.json"
RSS_URL = "https://www.foreks.com/rss/"
CONTENT_NS = {"content": "http://purl.org/rss/1.0/modules/content/"}
CHECK_INTERVAL_SEC = 600  # 10 dakika
REQUEST_TIMEOUT_SEC = 20
SEND_DELAY_SEC = 0.5  # ayrı mesajlar arasında Telegram'ı yormamak icin
MAX_SEEN = 500


def extract_content(encoded_html):
    """content:encoded alanındaki HTML'den (resim hariç) düz metni çıkarır."""
    text = re.sub(r"<figure>.*?</figure>", "", encoded_html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def fetch_rss_items():
    """RSS akışından haber listesini döner (akışın verdiği sırayla, en yeni
    genelde en üstte)."""
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        data = resp.read()
    root = ET.fromstring(data)

    items = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        encoded = item.findtext("content:encoded", namespaces=CONTENT_NS) or ""
        if title and link:
            items.append({"title": title, "link": link, "icerik": extract_content(encoded)})
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


def format_message(item):
    title = html.escape(item["title"])
    link = html.escape(item["link"], quote=True)
    lines = [f'<b>📰 <a href="{link}">{title}</a></b>']
    if item["icerik"]:
        lines.append("")
        lines.append(html.escape(item["icerik"]))
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
            print(f"{len(new_items)} yeni haber bulundu, ayrı ayrı gönderiliyor...")
            # RSS'te en yeni en üstte gelir, eskiden yeniye sırayla gönder.
            for it in reversed(new_items):
                send_telegram_message(token, chat_id, format_message(it))
                seen.append(it["link"])
                time.sleep(SEND_DELAY_SEC)
            save_seen(seen)
        else:
            print("Yeni haber yok.")

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
