#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BIST PİYASA HABERLERİ ALARMI
==============================
Foreks'in ekonomi RSS akışını periyodik olarak tarar (BIST'i doğrudan ya
da dolaylı etkileyebilecek şirket, faiz, TCMB, kur, küresel piyasa gibi
haberler). Yeni bir haber bulunca, başlığını, kısa içerik özetini ve
yerel Ollama modeliyle (ücretsiz, API maliyeti yok) çıkarılan BIST etki
analizini (Olumlu/Olumsuz/Notr yönü, 1-10 şiddet puanı, gerekçe) ve
haberin tarihi + o güne ait sıra numarasını ve günün kümülatif skorunu
(Olumlu +puan, Olumsuz -puan; gönderilmeyen haberler dahil TÜM haberler
sayılır) Telegram'a AYRI birer push mesajı olarak gönderir. Şiddet puanı HABER_MIN_SIDDET'in altında olan
haberler (önemsiz kabul edilip) gönderilmez - bilgisayar uzun süre
kapalı kalıp bir anda çok sayıda haber birikince spam'i azaltır. Ollama
çalışmıyorsa ya da çağrı başarısız olursa analiz sessizce atlanır,
haber yine de (filtresiz) gönderilir.

Gereksinim: Ollama kurulu ve çalışıyor olmalı (brew install ollama;
brew services start ollama), OLLAMA_MODEL'de tanımlı model indirilmiş
olmalı (ollama pull qwen2.5:7b-instruct).

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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from supertrend_alarm import ISTANBUL_TZ, load_telegram_config, send_telegram_message

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "haber_alarm_state.json"
GUN_SAYAC_FILE = BASE_DIR / "haber_gun_sayac.json"
RSS_URL = "https://www.foreks.com/rss/"
CONTENT_NS = {"content": "http://purl.org/rss/1.0/modules/content/"}
CHECK_INTERVAL_SEC = 600  # 10 dakika
REQUEST_TIMEOUT_SEC = 20
SEND_DELAY_SEC = 0.5  # ayrı mesajlar arasında Telegram'ı yormamak icin
MAX_SEEN = 500
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen2.5:7b-instruct"
OLLAMA_TIMEOUT_SEC = 90  # yerel model ilk yuklemede yavas olabilir
HABER_MIN_SIDDET = 6  # bu puanin altindaki haberler periyodik alarmda gonderilmez

YON_EMOJI = {"Olumlu": "🟢", "Olumsuz": "🔴", "Notr": "⚪"}


def extract_content(encoded_html):
    """content:encoded alanındaki HTML'den (resim hariç) düz metni çıkarır."""
    text = re.sub(r"<figure>.*?</figure>", "", encoded_html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def parse_pubdate(pubdate_str):
    """RSS pubDate metnini (RFC 2822) Istanbul saatine cevirir. Ayristirilamazsa None doner."""
    try:
        dt = parsedate_to_datetime(pubdate_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ISTANBUL_TZ)
    except Exception:
        return None


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
        tarih = parse_pubdate(item.findtext("pubDate") or "") or datetime.now(ISTANBUL_TZ)
        if title and link:
            items.append({
                "title": title,
                "link": link,
                "icerik": extract_content(encoded),
                "gun_str": tarih.strftime("%d.%m.%Y"),
            })
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


def load_gun_sayac():
    if GUN_SAYAC_FILE.exists():
        try:
            return json.loads(GUN_SAYAC_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_gun_sayac(sayac):
    GUN_SAYAC_FILE.write_text(json.dumps(sayac, ensure_ascii=False, indent=2))


def bos_gun_kaydi():
    return {"gonderilen": 0, "toplam": 0, "skor": 0, "olumlu": 0, "olumsuz": 0, "notr": 0}


def gun_kaydi(sayac, gun_str):
    """Gunun kaydini doner. Eski surumde sadece duz bir sayi tutuluyordu,
    o format da yeni yapiya tasinir."""
    kayit = sayac.get(gun_str)
    if isinstance(kayit, int):
        kayit = {**bos_gun_kaydi(), "gonderilen": kayit}
    elif not isinstance(kayit, dict):
        kayit = bos_gun_kaydi()
    sayac[gun_str] = kayit
    return kayit


def gune_isle(sayac, gun_str, analysis):
    """Haberi gunun kumulatif skoruna isler (mesaj gonderilsin gonderilmesin
    tum haberler sayilir). Olumlu +puan, Olumsuz -puan, Notr 0."""
    kayit = gun_kaydi(sayac, gun_str)
    kayit["toplam"] += 1
    if analysis:
        if analysis["yon"] == "Olumlu":
            kayit["skor"] += analysis["puan"]
            kayit["olumlu"] += 1
        elif analysis["yon"] == "Olumsuz":
            kayit["skor"] -= analysis["puan"]
            kayit["olumsuz"] += 1
        else:
            kayit["notr"] += 1
    return kayit


def next_gun_no(kayit):
    """Gonderilen haber sayacini artirip yeni degeri doner."""
    kayit["gonderilen"] += 1
    return kayit["gonderilen"]


def format_gun_ozet(kayit):
    skor = kayit["skor"]
    emoji = "🟢" if skor > 0 else ("🔴" if skor < 0 else "⚪")
    isaret = "+" if skor > 0 else ""
    return (f"📊 Gün skoru: {emoji} {isaret}{skor} | {kayit['toplam']} haber "
            f"({kayit['olumlu']}🟢 {kayit['olumsuz']}🔴 {kayit['notr']}⚪)")


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
    (1-10) yerel Ollama modeline degerlendirtir. Ollama calismiyorsa ya
    da cagri basarisiz olursa None doner - haber yine de analizsiz
    gosterilir."""
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
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_SEC) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        text = result.get("message", {}).get("content", "")
        return parse_analysis(text)
    except Exception as e:
        print(f"Ollama analiz hatası: {e}")
        return None


def format_message(item, analysis=None, gun_no=None, gun_kaydi_=None):
    title = html.escape(item["title"])
    link = html.escape(item["link"], quote=True)
    lines = []
    if item.get("gun_str") and gun_no is not None:
        lines.append(f"📅 {item['gun_str']} — Haber #{gun_no}")
    lines.append(f'<b>📰 <a href="{link}">{title}</a></b>')
    if item["icerik"]:
        lines.append("")
        lines.append(html.escape(item["icerik"]))
    if analysis:
        emoji = YON_EMOJI.get(analysis["yon"], "⚪")
        lines.append("")
        lines.append(f"{emoji} <b>{analysis['yon']}</b> — Şiddet: {analysis['puan']}/10")
        if analysis["gerekce"]:
            lines.append(html.escape(analysis["gerekce"]))
    if gun_kaydi_:
        lines.append("")
        lines.append(format_gun_ozet(gun_kaydi_))
    return "\n".join(lines)


def main():
    token, chat_id = load_telegram_config()
    if not token or not chat_id:
        print("UYARI: Telegram ayarı yok, haber alarmı başlatılamıyor.")
        return

    print("Haber alarmı başladı, RSS periyodik olarak taranacak...")
    seen = load_seen()
    sayac = load_gun_sayac()
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
            print(f"{len(new_items)} yeni haber bulundu, değerlendiriliyor...")
            # RSS'te en yeni en üstte gelir, eskiden yeniye sırayla işle.
            gonderilen = 0
            for it in reversed(new_items):
                analysis = analyze_impact(it)
                seen.append(it["link"])
                # Gonderilmeyen haberler de gunun kumulatif skoruna dahil edilir.
                kayit = gune_isle(sayac, it["gun_str"], analysis)
                if analysis and analysis["puan"] < HABER_MIN_SIDDET:
                    print(f"  Düşük şiddet ({analysis['puan']}/10), atlandı: {it['title'][:50]}")
                    continue
                gun_no = next_gun_no(kayit)
                send_telegram_message(token, chat_id, format_message(it, analysis, gun_no, kayit))
                gonderilen += 1
                time.sleep(SEND_DELAY_SEC)
            print(f"  {gonderilen}/{len(new_items)} haber gönderildi (şiddet < {HABER_MIN_SIDDET} olanlar atlandı).")
            save_seen(seen)
            save_gun_sayac(sayac)
        else:
            now_str = datetime.now(ISTANBUL_TZ).strftime("%Y-%m-%d %H:%M")
            print(f"Yeni haber yok. ({now_str})")
            send_telegram_message(token, chat_id, f"🔍 Tarama: {now_str} — yeni haber yok.")

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
