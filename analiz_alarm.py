#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BIST 30 ÇOKLU ZAMAN DİLİMİ ANALİZ ALARMI
=========================================
BIST 30 hisselerini CHECK_INTERVAL_SEC saniyede bir (varsayılan 10 dakika)
tarar ve şu şartların hepsini sağlayan hisseleri Telegram'a bildirir:

  - 5dk, 15dk, 1s ve 2s zaman dilimlerinin HEPSİNDE Supertrend AL yönünde
  - Aynı zaman dilimlerinin HEPSİNDE RSI teyit ediyor (50-70 arası)
  - Bu zaman dilimlerinden EN AZ BİRİNDE yüksek hacim (ortalamanın 2 katı)

Hacim şartının tek bir zaman diliminde aranmasının sebebi: hacim patlaması
anlık bir olaydır, 5dk ve 2s gibi iç içe geçmiş pencerelerde aynı anda 2x
hacim görmek pratikte neredeyse imkânsızdır.

Aynı hisse için şart sağlandığı sürece tekrar tekrar mesaj atmaz; şart
bozulup yeniden sağlandığında yeniden bildirir (bkz. analiz_alarm_state.json).

Sadece BIST işlem saatlerinde (hafta içi 10:00-18:30) tarar, dışında bekler.

KULLANIM
--------
    python3 analiz_alarm.py
"""

import json
import time
from datetime import datetime
from pathlib import Path

from supertrend_alarm import (
    BIST30_TICKERS,
    ISTANBUL_TZ,
    REQUEST_DELAY_SEC,
    get_timeframe_status,
    load_telegram_config,
    send_telegram_message,
)

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "analiz_alarm_state.json"

ANALIZ_TIMEFRAMES = ("5dk", "15dk", "1s", "2s")
TIMEFRAME_ADLARI = {"5dk": "5m", "15dk": "15m", "1s": "1h", "2s": "2h"}
CHECK_INTERVAL_SEC = 600  # 10 dakika
PIYASA_ACILIS = 10        # BIST islem saatleri (Istanbul)
PIYASA_KAPANIS = 18       # kapanis mumu da degerlendirilsin diye 18:30'a kadar taranir


def piyasa_acik(now=None):
    """BIST islem saatlerinde miyiz (hafta ici 10:00-18:30)."""
    now = now or datetime.now(ISTANBUL_TZ)
    if now.weekday() > 4:
        return False
    if now.hour < PIYASA_ACILIS:
        return False
    return now.hour < PIYASA_KAPANIS or (now.hour == PIYASA_KAPANIS and now.minute <= 30)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def hisseyi_degerlendir(ticker):
    """Hisseyi ANALIZ_TIMEFRAMES uzerinde degerlendirir.
    Doner: (uygun_mu, durumlar) - durumlar zaman dilimi -> status sozlugu."""
    durumlar = {}
    for label in ANALIZ_TIMEFRAMES:
        durumlar[label] = get_timeframe_status(ticker, label)
        time.sleep(REQUEST_DELAY_SEC)

    if not all(durumlar.values()):
        return False, durumlar

    hepsi_al = all(durumlar[tf]["yon"] == 1 for tf in ANALIZ_TIMEFRAMES)
    hepsi_rsi = all(durumlar[tf]["rsi_uygun"] for tf in ANALIZ_TIMEFRAMES)
    hacim_var = any(durumlar[tf]["yuksek_hacim"] for tf in ANALIZ_TIMEFRAMES)
    return (hepsi_al and hepsi_rsi and hacim_var), durumlar


def format_signal(ticker, durumlar):
    ilk = durumlar[ANALIZ_TIMEFRAMES[0]]
    now_str = datetime.now(ISTANBUL_TZ).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"<b>🚨 ANALİZ SİNYALİ: {ticker}</b>",
        f"Güncel fiyat: {ilk['kapanis']:.2f} — {now_str}",
        "",
    ]
    for label in ANALIZ_TIMEFRAMES:
        s = durumlar[label]
        hacim = " 🔥" if s["yuksek_hacim"] else ""
        lines.append(
            f"🟢 {TIMEFRAME_ADLARI[label]}: ST {s['supertrend']:.2f} "
            f"(%{s['mesafe_pct']:.1f}) RSI {s['rsi']:.0f}{hacim}"
        )
    lines.append("")
    lines.append("Tüm zaman dilimlerinde AL + RSI teyitli, hacim girişi var.")
    lines.append("Yatırım tavsiyesi değildir.")
    return "\n".join(lines)


def main():
    token, chat_id = load_telegram_config()
    if not token or not chat_id:
        print("UYARI: Telegram ayarı yok, analiz alarmı başlatılamıyor.")
        return

    print(f"Analiz alarmı başladı ({', '.join(TIMEFRAME_ADLARI[t] for t in ANALIZ_TIMEFRAMES)}), "
          f"{CHECK_INTERVAL_SEC // 60} dakikada bir taranacak...")
    state = load_state()

    while True:
        if not piyasa_acik():
            print(f"Piyasa kapalı ({datetime.now(ISTANBUL_TZ):%Y-%m-%d %H:%M}), taranmıyor.")
            time.sleep(CHECK_INTERVAL_SEC)
            continue

        print(f"Tarama başlıyor ({datetime.now(ISTANBUL_TZ):%H:%M})...")
        uyanlar = []
        for ticker in BIST30_TICKERS:
            try:
                uygun, durumlar = hisseyi_degerlendir(ticker)
            except Exception as e:
                print(f"  {ticker}: hata - {e}")
                continue

            zaten_bildirildi = state.get(ticker) == "aktif"
            if uygun and not zaten_bildirildi:
                print(f"  {ticker}: SINYAL (yeni)")
                send_telegram_message(token, chat_id, format_signal(ticker, durumlar))
                state[ticker] = "aktif"
                uyanlar.append(ticker)
            elif uygun:
                print(f"  {ticker}: sinyal sürüyor (zaten bildirildi)")
            elif zaten_bildirildi:
                # Sart bozuldu: bir dahaki saglanista tekrar bildirilsin.
                print(f"  {ticker}: şart bozuldu, sıfırlandı")
                state.pop(ticker, None)

        save_state(state)
        aktif = [t for t, v in state.items() if v == "aktif"]
        print(f"Tarama bitti. Yeni sinyal: {len(uyanlar)} | Aktif sinyal: {len(aktif)} {aktif}")

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
