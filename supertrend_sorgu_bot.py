#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BIST 30 SUPERTREND - ANLIK SORGU BOTU
=======================================
Telegram botuna gelen /durum komutlarını dinler, o anki (en son kapanmış
1 saatlik mumdaki) Supertrend durumunu hesaplayıp cevap olarak gönderir.

supertrend_alarm.py'deki periyodik tarama scriptinden BAĞIMSIZDIR - o
sadece YENİ AL sinyali bulunca mesaj atar, bu ise istediğin an mevcut
durumu sorgulamanı sağlar. Sürekli çalışan ayrı bir süreçtir (launchd
KeepAlive ile arka planda hep açık tutulur).

KOMUTLAR (Telegram'dan bota yaz)
---------------------------------
    /durum          -> BIST 30 + izleme listesindeki hisselerin anlık (1S) trend yönü
    /durum THYAO    -> tek bir hissenin anlık (1S) detayı
    /coklu THYAO    -> tek bir hissenin 5dk/15dk/1s/4s/1g/1hf Supertrend durumu
    /izle           -> izleme listesini göster
    /izle EKLE X    -> X hissesini izleme listesine ekle (BIST'te işlem gören her hisse)
    /izle SIL X     -> X hissesini izleme listesinden çıkar
    /liste          -> BIST 30 + izleme listesini tüm zaman dilimlerinde tarar,
                        şu an en çok zaman diliminde AL bölgesinde olan
                        hisseleri sıralar (0-6 arası puan, kendi seçer)
    /yardim         -> komut listesini gösterir

Güvenlik: sadece telegram_config.json'daki chat_id'den gelen komutlara
cevap verir, başka biri botu bulup yazsa bile yanıt almaz.

KULLANIM
--------
    python3 supertrend_sorgu_bot.py
"""

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from supertrend_alarm import (
    ISTANBUL_TZ,
    REQUEST_DELAY_SEC,
    TIMEFRAME_CONFIG,
    TIMEFRAME_LABELS_ORDERED,
    compute_supertrend,
    get_closed_candles,
    get_scan_universe,
    get_timeframe_status,
    load_telegram_config,
    load_watchlist,
    save_watchlist,
    send_telegram_message,
)

TIMEFRAME_NAMES = {
    "5dk": "5 Dakika", "15dk": "15 Dakika", "1s": "1 Saat",
    "4s": "4 Saat", "1g": "1 Gün", "1hf": "1 Hafta",
}

HELP_TEXT = (
    "<b>🤖 Supertrend Sorgu Botu - Komutlar</b>\n\n"
    "<b>/durum</b>\n"
    "BIST 30 + izleme listesindeki tüm hisselerin anlık (1 saatlik) trend yönünü listeler.\n\n"
    "<b>/durum HISSE</b>  (örn. /durum THYAO)\n"
    "Tek bir hissenin anlık (1 saatlik) Supertrend detayını gösterir.\n\n"
    "<b>/coklu HISSE</b>  (örn. /coklu THYAO)\n"
    "Bir hissenin 5dk/15dk/1s/4s/1g/1hf zaman dilimlerindeki Supertrend seviyelerini ve yönünü tek mesajda gösterir.\n\n"
    "<b>/izle</b>\n"
    "İzleme listesindeki hisseleri gösterir.\n\n"
    "<b>/izle EKLE HISSE</b>\n"
    "BIST'te işlem gören herhangi bir hisseyi izleme listesine ekler (bu hisse hem /durum taramasına hem periyodik AL sinyali alarmına dahil olur).\n\n"
    "<b>/izle SIL HISSE</b>\n"
    "Hisseyi izleme listesinden çıkarır.\n\n"
    "<b>/liste</b>\n"
    "BIST 30 + izleme listesindeki hisseleri 6 zaman diliminin (5dk/15dk/1s/4s/1g/1hf) tamamında tarar; şu anki fiyata göre en çok zaman diliminde AL bölgesinde olanları kendi sıralayıp gösterir (0-6 puan, birkaç dakika sürebilir).\n\n"
    "<b>/yardim</b>\n"
    "Bu mesajı gösterir."
)

BASE_DIR = Path(__file__).resolve().parent
OFFSET_FILE = BASE_DIR / "supertrend_sorgu_offset.json"
POLL_TIMEOUT_SEC = 30


def load_offset():
    if OFFSET_FILE.exists():
        try:
            return json.loads(OFFSET_FILE.read_text()).get("offset", 0)
        except Exception:
            return 0
    return 0


def save_offset(offset):
    OFFSET_FILE.write_text(json.dumps({"offset": offset}))


def get_updates(token, offset):
    url = (f"https://api.telegram.org/bot{token}/getUpdates"
           f"?timeout={POLL_TIMEOUT_SEC}&offset={offset}")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT_SEC + 10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def current_status(ticker):
    """Ticker icin en son kapanmis (1 saatlik) mumdaki anlik Supertrend durumu."""
    interval, range_ = TIMEFRAME_CONFIG["1s"]
    candles = get_closed_candles(ticker, interval, range_)
    st = compute_supertrend(candles)
    if not st:
        return None
    dt, close, st_val, direction = st[-1]
    return {
        "ticker": ticker,
        "mum_zamani": dt.astimezone(ISTANBUL_TZ),
        "kapanis": close,
        "supertrend": st_val,
        "yon": direction,
        "mesafe_pct": (close - st_val) / st_val * 100.0,
    }


def format_single(s):
    yon_str = "🟢 YUKARI (AL bölgesi)" if s["yon"] == 1 else "🔴 AŞAĞI (SAT bölgesi)"
    return (
        f"<b>{s['ticker']}</b>\n"
        f"Yön: {yon_str}\n"
        f"Kapanış: {s['kapanis']:.2f}\n"
        f"Supertrend: {s['supertrend']:.2f}\n"
        f"Çizgiye mesafe: %{s['mesafe_pct']:.2f}\n"
        f"Son mum: {s['mum_zamani'].strftime('%Y-%m-%d %H:%M')}"
    )


def format_all(results):
    up = sorted([r for r in results if r["yon"] == 1], key=lambda r: r["mesafe_pct"])
    down = sorted([r for r in results if r["yon"] == -1], key=lambda r: abs(r["mesafe_pct"]))

    now_str = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    lines = ["<b>📊 BIST 30 - Anlık Supertrend Durumu (1S)</b>", now_str, ""]
    lines.append(f"🟢 Yükseliş trendinde ({len(up)}):")
    for r in up:
        lines.append(f"  {r['ticker']} — {r['kapanis']:.2f} (> %{r['mesafe_pct']:.1f} yüksek)")
    lines.append("")
    lines.append(f"🔴 Düşüş trendinde ({len(down)}, AL'a en yakın üstte):")
    for r in down:
        lines.append(f"  {r['ticker']} — {r['kapanis']:.2f} (> %{abs(r['mesafe_pct']):.1f} düşük)")
    return "\n".join(lines)


def handle_durum(token, chat_id, arg):
    if arg:
        ticker = arg.strip().upper()
        s = current_status(ticker)
        if not s:
            send_telegram_message(token, chat_id, f"'{ticker}' için veri alınamadı, hisse kodunu kontrol et.")
            return
        send_telegram_message(token, chat_id, format_single(s))
    else:
        send_telegram_message(token, chat_id, "Taranıyor, birkaç saniye sürecek...")
        results = []
        for t in get_scan_universe():
            s = current_status(t)
            time.sleep(REQUEST_DELAY_SEC)
            if s:
                results.append(s)
        send_telegram_message(token, chat_id, format_all(results))


def format_multi(ticker, results):
    guncel_fiyat = next((s["kapanis"] for label in TIMEFRAME_LABELS_ORDERED
                          if (s := results.get(label))), None)
    lines = [f"<b>⏱ {ticker} - Çoklu Zaman Dilimi Supertrend</b>"]
    if guncel_fiyat is not None:
        lines.append(f"Güncel fiyat: {guncel_fiyat:.2f}")
    lines.append("")
    for label in TIMEFRAME_LABELS_ORDERED:
        s = results.get(label)
        name = TIMEFRAME_NAMES[label]
        if not s:
            lines.append(f"{name}: veri yok")
            continue
        yon_emoji = "🟢" if s["yon"] == 1 else "🔴"
        lines.append(f"{yon_emoji} {name}: ST {s['supertrend']:.2f} (fiyata %{s['mesafe_pct']:.1f})")
    return "\n".join(lines)


def handle_coklu(token, chat_id, arg):
    if not arg:
        send_telegram_message(token, chat_id, "Kullanım: /coklu HISSE (örn. /coklu THYAO)")
        return
    ticker = arg.strip().upper()
    send_telegram_message(token, chat_id, f"{ticker} taranıyor...")
    results = {}
    for label in TIMEFRAME_LABELS_ORDERED:
        results[label] = get_timeframe_status(ticker, label)
        time.sleep(REQUEST_DELAY_SEC)
    if all(v is None for v in results.values()):
        send_telegram_message(token, chat_id, f"'{ticker}' için veri alınamadı, hisse kodunu kontrol et.")
        return
    send_telegram_message(token, chat_id, format_multi(ticker, results))


def handle_izle(token, chat_id, arg):
    parts = arg.split()
    if not parts:
        watchlist = load_watchlist()
        if not watchlist:
            send_telegram_message(token, chat_id, "İzleme listesi boş. Eklemek için: /izle EKLE HISSE")
        else:
            send_telegram_message(token, chat_id, "<b>👁 İzleme Listesi</b>\n" + "\n".join(watchlist))
        return

    action = parts[0].upper()
    if action == "EKLE" and len(parts) >= 2:
        ticker = parts[1].strip().upper()
        watchlist = load_watchlist()
        if ticker in watchlist:
            send_telegram_message(token, chat_id, f"{ticker} zaten izleme listesinde.")
            return
        interval, range_ = TIMEFRAME_CONFIG["1g"]
        if not get_closed_candles(ticker, interval, range_):
            send_telegram_message(token, chat_id, f"'{ticker}' için veri bulunamadı, hisse kodunu kontrol et.")
            return
        watchlist.append(ticker)
        save_watchlist(watchlist)
        send_telegram_message(token, chat_id, f"{ticker} izleme listesine eklendi.")
    elif action == "SIL" and len(parts) >= 2:
        ticker = parts[1].strip().upper()
        watchlist = load_watchlist()
        if ticker not in watchlist:
            send_telegram_message(token, chat_id, f"{ticker} izleme listesinde değil.")
            return
        watchlist.remove(ticker)
        save_watchlist(watchlist)
        send_telegram_message(token, chat_id, f"{ticker} izleme listesinden silindi.")
    else:
        send_telegram_message(
            token, chat_id,
            "Kullanım:\n/izle - listeyi göster\n/izle EKLE HISSE\n/izle SIL HISSE",
        )


def format_liste(ranked, top_n=10):
    shown = [item for item in ranked if item[1] > 0][:top_n]
    n_tf = len(TIMEFRAME_LABELS_ORDERED)
    lines = [
        "<b>🏆 En Çok Zaman Diliminde AL Bölgesinde Olan Hisseler</b>",
        f"(şu an 5dk/15dk/1s/4s/1g/1hf içinden kaçında AL, en yüksek {n_tf}/{n_tf})",
        "",
    ]
    if not shown:
        lines.append("Şu an hiçbir hissede AL sinyali yok.")
    else:
        for i, (ticker, score) in enumerate(shown, 1):
            lines.append(f"{i}. {ticker} — {score}/{n_tf}")
    return "\n".join(lines)


def handle_liste(token, chat_id):
    universe = get_scan_universe()
    send_telegram_message(
        token, chat_id,
        f"{len(universe)} hisse, 6 zaman diliminde taranıyor, bu birkaç dakika sürebilir...",
    )
    scores = {}
    for ticker in universe:
        score = 0
        for label in TIMEFRAME_LABELS_ORDERED:
            s = get_timeframe_status(ticker, label)
            time.sleep(REQUEST_DELAY_SEC)
            if s and s["yon"] == 1:
                score += 1
        scores[ticker] = score
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    send_telegram_message(token, chat_id, format_liste(ranked))


def main():
    token, chat_id = load_telegram_config()
    if not token or not chat_id:
        print("UYARI: Telegram ayari yok, dinleyici baslatilamiyor.")
        return

    print("Anlik sorgu botu dinlemeye basladi (/durum, /durum HISSE)...")
    offset = load_offset()

    while True:
        try:
            payload = get_updates(token, offset)
        except Exception as e:
            print(f"getUpdates hatasi: {e}")
            time.sleep(5)
            continue

        for update in payload.get("result", []):
            offset = update["update_id"] + 1
            save_offset(offset)

            message = update.get("message") or update.get("edited_message")
            if not message:
                continue
            msg_chat_id = str(message.get("chat", {}).get("id"))
            if msg_chat_id != str(chat_id):
                continue  # yetkisiz kullanicidan gelen komutlari yoksay

            text = (message.get("text") or "").strip()
            if text == "/durum" or text.startswith("/durum "):
                arg = text[len("/durum"):].strip()
                print(f"Komut alindi: /durum {arg}")
                try:
                    handle_durum(token, chat_id, arg)
                except Exception as e:
                    print(f"Komut isleme hatasi: {e}")
                    send_telegram_message(token, chat_id, "Sorgu sirasinda bir hata olustu.")
            elif text == "/coklu" or text.startswith("/coklu "):
                arg = text[len("/coklu"):].strip()
                print(f"Komut alindi: /coklu {arg}")
                try:
                    handle_coklu(token, chat_id, arg)
                except Exception as e:
                    print(f"Komut isleme hatasi: {e}")
                    send_telegram_message(token, chat_id, "Sorgu sirasinda bir hata olustu.")
            elif text == "/izle" or text.startswith("/izle "):
                arg = text[len("/izle"):].strip()
                print(f"Komut alindi: /izle {arg}")
                try:
                    handle_izle(token, chat_id, arg)
                except Exception as e:
                    print(f"Komut isleme hatasi: {e}")
                    send_telegram_message(token, chat_id, "Sorgu sirasinda bir hata olustu.")
            elif text == "/liste":
                print("Komut alindi: /liste")
                try:
                    handle_liste(token, chat_id)
                except Exception as e:
                    print(f"Komut isleme hatasi: {e}")
                    send_telegram_message(token, chat_id, "Sorgu sirasinda bir hata olustu.")
            elif text in ("/yardim", "/help", "/start"):
                print("Komut alindi: /yardim")
                send_telegram_message(token, chat_id, HELP_TEXT)
            elif text.startswith("/"):
                print(f"Bilinmeyen komut: {text}")
                send_telegram_message(token, chat_id, "Bilinmeyen komut. Komutları görmek için /yardim yaz.")


if __name__ == "__main__":
    main()
