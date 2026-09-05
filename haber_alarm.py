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
from datetime import datetime
from pathlib import Path

from supertrend_alarm import ISTANBUL_TZ, load_telegram_config, send_telegram_message

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "haber_alarm_state.json"
ANTHROPIC_CONFIG_FILE = BASE_DIR / "anthropic_config.json"
RSS_URL = "https://www.foreks.com/rss/"
CONTENT_NS = {"content": "http://purl.org/rss/1.0/modules/content/"}
CHECK_INTERVAL_SEC = 600  # 10 dakika
REQUEST_TIMEOUT_SEC = 20
SEND_DELAY_SEC = 0.5  # ayrı mesajlar arasında Telegram'ı yormamak icin
MAX_SEEN = 500
ANALYSIS_MODEL = "claude-opus-5"

YON_EMOJI = {"Olumlu": "🟢", "Olumsuz": "🔴", "Notr": "⚪"}


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


def load_anthropic_key():
    if ANTHROPIC_CONFIG_FILE.exists():
        try:
            return json.loads(ANTHROPIC_CONFIG_FILE.read_text()).get("api_key")
        except Exception:
            return None
    return None


def parse_analysis(text):
    yon_m = re.search(r"YON:\s*(Olumlu|Olumsuz|Notr)", text, re.IGNORECASE)
    puan_m = re.search(r"PUAN:\s*(\d+)", text)
    gerekce_m = re.search(r"GEREKCE:\s*(.+)", text, re.IGNORECASE)
    if not yon_m or not puan_m:
        return None
    yon = yon_m.group(1).capitalize()
    puan = max(1, min(10, int(puan_m.group(1))))
    gerekce = gerekce_m.group(1).strip() if gerekce_m else ""
    return {"yon": yon, "puan": puan, "gerekce": gerekce}


def analyze_impact(item):
    """Haberin Borsa Istanbul icin olumlu/olumsuz oldugunu ve siddetini
    (1-10) Claude'a degerlendirtir. API anahtari yoksa ya da cagri
    basarisiz olursa None doner - haber yine de analizsiz gosterilir."""
    api_key = load_anthropic_key()
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        prompt = (
            "Aşağıdaki ekonomi/piyasa haberini oku ve Borsa İstanbul (BIST) "
            "genel endeksi açısından değerlendir.\n\n"
            f"Başlık: {item['title']}\n"
            f"İçerik: {item['icerik']}\n\n"
            "Tam olarak şu formatta, başka hiçbir şey eklemeden cevap ver:\n"
            "YON: Olumlu / Olumsuz / Notr\n"
            "PUAN: 1-10 arası bir sayı (şiddeti - 1 çok hafif, 10 çok şiddetli/önemli)\n"
            "GEREKCE: tek cümlelik kısa açıklama"
        )
        response = client.messages.create(
            model=ANALYSIS_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return parse_analysis(text)
    except Exception as e:
        print(f"Claude analiz hatası: {e}")
        return None


def format_message(item, analysis=None):
    title = html.escape(item["title"])
    link = html.escape(item["link"], quote=True)
    lines = [f'<b>📰 <a href="{link}">{title}</a></b>']
    if item["icerik"]:
        lines.append("")
        lines.append(html.escape(item["icerik"]))
    if analysis:
        emoji = YON_EMOJI.get(analysis["yon"], "⚪")
        lines.append("")
        lines.append(f"{emoji} <b>{analysis['yon']}</b> — Şiddet: {analysis['puan']}/10")
        if analysis["gerekce"]:
            lines.append(html.escape(analysis["gerekce"]))
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
            now_str = datetime.now(ISTANBUL_TZ).strftime("%Y-%m-%d %H:%M")
            print(f"Yeni haber yok. ({now_str})")
            send_telegram_message(token, chat_id, f"🔍 Tarama: {now_str} — yeni haber yok.")

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
