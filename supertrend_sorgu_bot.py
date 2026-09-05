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
    /durum          -> BIST 30'daki tüm hisselerin anlık (1S) trend yönü
    /durum THYAO    -> tek bir hissenin 5dk/15dk/1s/4s/1g/1hf Supertrend durumu
    /liste          -> BIST 30'u tüm zaman dilimlerinde tarar, şu an en çok
                        zaman diliminde AL bölgesinde olan hisseleri sıralar
                        (0-6 arası puan, kendi seçer)
    /firsat         -> 1g'de sert düşmüş ama 1s'de AL'a dönmüş ve hacim
                        girişi olan (fırsat) hisseleri tarar
    /gunici         -> 5dk+15dk+1s hepsi AL, hacim girişi ve RSI teyidi
                        olan gün içi trade adaylarını tarar
    /haber          -> son ekonomi haberlerini tek tek, ayrı mesajlar
                        halinde gönderir (liste değil)
    /start          -> tanıtım/giriş mesajını gösterir
    /help           -> komut listesini gösterir

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

from haber_alarm import analyze_impact, fetch_rss_items, format_message as format_haber_message
from supertrend_alarm import (
    BIST30_TICKERS,
    REQUEST_DELAY_SEC,
    TIMEFRAME_LABELS_ORDERED,
    get_timeframe_status,
    load_telegram_config,
    send_telegram_message,
)

HABER_LIMIT = 10
HABER_SEND_DELAY_SEC = 0.5

TIMEFRAME_NAMES = {
    "5dk": "5m", "15dk": "15m", "1s": "1h",
    "4s": "4h", "1g": "1d", "1hf": "1w",
}

WELCOME_TEXT = (
    "<b>🤖 Supertrend Sorgu Botu</b>\n\n"
    "Bu bot ALPER GÜL tarafından yaratılmıştır, hisse taraması amacıyla "
    "kullanılmakta olup yatırım tavsiyesi içermez. Tüm hakları saklıdır.\n\n"
    "Komutlar için /help yaz."
)

HELP_TEXT = (
    "<b>🤖 Supertrend Sorgu Botu - Komutlar</b>\n\n"
    "<b>/durum</b>\n"
    "BIST 30'daki tüm hisselerin anlık (1 saatlik) trend yönünü listeler.\n\n"
    "<b>/durum HISSE</b>  (örn. /durum THYAO)\n"
    "Bir hissenin 5dk/15dk/1s/4s/1g/1hf zaman dilimlerindeki Supertrend seviyelerini ve yönünü tek mesajda gösterir.\n\n"
    "<b>/liste</b>\n"
    "BIST 30'u 6 zaman diliminin (5dk/15dk/1s/4s/1g/1hf) tamamında tarar; şu anki fiyata göre en çok zaman diliminde AL bölgesinde olanları kendi sıralayıp gösterir (0-6 puan, birkaç dakika sürebilir).\n\n"
    "<b>/firsat</b>\n"
    "BIST 30'u tarar; 1 günlükte çizginin en az %5 altında (sert düşmüş) ama 1 saatlikte AL'a dönmüş ve kısa vadede (5dk/15dk/1s) hacim girişi olan hisseleri listeler (birkaç dakika sürebilir).\n\n"
    "<b>/gunici</b>\n"
    "BIST 30'u tarar; 5dk+15dk+1s'in ÜÇÜ BİRDEN AL'da olan, en az birinde hacim girişi ve en az birinde RSI teyidi olan gün içi trade adaylarını listeler (geçmiş trende bakmaz, sadece anlık gücü ölçer, birkaç dakika sürebilir).\n\n"
    "<b>/haber</b>\n"
    f"Son {HABER_LIMIT} ekonomi haberini tek tek, ayrı mesajlar halinde gönderir (liste olarak değil). "
    "Her haberin altına BIST için olumlu/olumsuz yönü, 10 üzerinden şiddeti ve kısa gerekçesi eklenir.\n\n"
    "<b>/help</b>\n"
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


def format_all(results):
    up = sorted([r for r in results if r["yon"] == 1], key=lambda r: r["mesafe_pct"], reverse=True)

    now_str = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    lines = ["<b>📊 BIST 30 - Yükseliş Trendinde Olanlar (1S)</b>", now_str, ""]
    for r in up:
        rsi = f" RSI {r['rsi']:.0f}" if r["rsi"] is not None else ""
        hacim = " 🔥" if r["yuksek_hacim"] else ""
        tik = " ✅" if (r["rsi_uygun"] and r["yuksek_hacim"]) else ""
        lines.append(f"  {r['ticker']} — {r['kapanis']:.2f} (%{r['mesafe_pct']:.1f}){rsi}{hacim}{tik}")
    return "\n".join(lines)


def format_multi(ticker, results):
    guncel_fiyat = next((s["kapanis"] for label in TIMEFRAME_LABELS_ORDERED
                          if (s := results.get(label))), None)
    lines = [f"<b>⏱ {ticker} - Supertrend Durumu</b>"]
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
        hacim = " 🔥" if s["yuksek_hacim"] else ""
        rsi = f" RSI {s['rsi']:.0f}" if s["rsi"] is not None else ""
        tik = " ✅" if (s["rsi_uygun"] and s["yuksek_hacim"]) else ""
        lines.append(f"{yon_emoji} {name}: ST {s['supertrend']:.2f} (%{s['mesafe_pct']:.1f}){rsi}{hacim}{tik}")
    return "\n".join(lines)


def handle_durum(token, chat_id, arg):
    if arg:
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
    else:
        send_telegram_message(token, chat_id, "Taranıyor, birkaç saniye sürecek...")
        results = []
        for t in BIST30_TICKERS:
            s = get_timeframe_status(t, "1s")
            time.sleep(REQUEST_DELAY_SEC)
            if s:
                results.append(s)
        send_telegram_message(token, chat_id, format_all(results))


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
    universe = BIST30_TICKERS
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


FIRSAT_UZAK_ESIK = -5.0  # 1g'de cizginin en az bu kadar altinda olmali (sert dusus)


def find_firsatlar(universe):
    """Uzun vadede (1g) sert dusmus ama kisa vadede (1s) AL'a donmus ve
    hacim girisi olan hisseleri bulur. Doner: (ticker, uzak, yakin) listesi,
    en sert dusenden en hafife siralanmis."""
    firsatlar = []
    for ticker in universe:
        durumlar = {}
        for label in ("5dk", "15dk", "1s", "1g"):
            durumlar[label] = get_timeframe_status(ticker, label)
            time.sleep(REQUEST_DELAY_SEC)

        uzak = durumlar["1g"]
        yakin = durumlar["1s"]
        if not uzak or not yakin:
            continue
        sert_dusmus = uzak["yon"] == -1 and uzak["mesafe_pct"] <= FIRSAT_UZAK_ESIK
        al_a_donmus = yakin["yon"] == 1
        para_girisi = any(
            durumlar[tf] and durumlar[tf]["yuksek_hacim"] for tf in ("5dk", "15dk", "1s")
        )
        if sert_dusmus and al_a_donmus and para_girisi:
            firsatlar.append((ticker, uzak, yakin))

    firsatlar.sort(key=lambda x: x[1]["mesafe_pct"])
    return firsatlar


def format_firsatlar(firsatlar):
    lines = [
        "<b>💎 Fırsat Listesi</b>",
        "(1g'de sert düşmüş, 1s'de AL'a dönmüş, hacim girişi var)",
        "",
    ]
    if not firsatlar:
        lines.append("Şu an kriterlere uyan hisse yok.")
    else:
        for ticker, uzak, yakin in firsatlar:
            lines.append(
                f"• <b>{ticker}</b> — 1g: %{uzak['mesafe_pct']:.1f} | "
                f"1s: %{yakin['mesafe_pct']:.1f} 🔥"
            )
    return "\n".join(lines)


def handle_firsat(token, chat_id):
    universe = BIST30_TICKERS
    send_telegram_message(
        token, chat_id,
        f"{len(universe)} hisse taranıyor, bu birkaç dakika sürebilir...",
    )
    firsatlar = find_firsatlar(universe)
    send_telegram_message(token, chat_id, format_firsatlar(firsatlar))


GUNICI_TIMEFRAMES = ("5dk", "15dk", "1s")


def find_gunici_firsatlari(universe):
    """Kısa vadede (5dk+15dk+1s) tam hizalanmış (hepsi AL), hacim girişi
    olan ve RSI teyidi olan gün içi trade adaylarını bulur. Döner:
    (ticker, durumlar) listesi, en güçlü 1s momentumundan en zayıfa."""
    adaylar = []
    for ticker in universe:
        durumlar = {}
        for label in GUNICI_TIMEFRAMES:
            durumlar[label] = get_timeframe_status(ticker, label)
            time.sleep(REQUEST_DELAY_SEC)
        if not all(durumlar.values()):
            continue
        hepsi_al = all(durumlar[tf]["yon"] == 1 for tf in GUNICI_TIMEFRAMES)
        hacim_var = any(durumlar[tf]["yuksek_hacim"] for tf in GUNICI_TIMEFRAMES)
        rsi_teyit = any(durumlar[tf]["rsi_uygun"] for tf in GUNICI_TIMEFRAMES)
        if hepsi_al and hacim_var and rsi_teyit:
            adaylar.append((ticker, durumlar))

    adaylar.sort(key=lambda x: x[1]["1s"]["mesafe_pct"], reverse=True)
    return adaylar


def format_gunici(adaylar):
    lines = [
        "<b>⚡ Gün İçi Fırsatları</b>",
        "(5dk+15dk+1s hepsi AL, hacim girişi + RSI teyidi var)",
        "",
    ]
    if not adaylar:
        lines.append("Şu an kriterlere uyan hisse yok.")
    else:
        for ticker, d in adaylar:
            lines.append(
                f"• <b>{ticker}</b> — 5dk: %{d['5dk']['mesafe_pct']:.1f} | "
                f"15dk: %{d['15dk']['mesafe_pct']:.1f} | 1s: %{d['1s']['mesafe_pct']:.1f} 🔥"
            )
    return "\n".join(lines)


def handle_gunici(token, chat_id):
    universe = BIST30_TICKERS
    send_telegram_message(
        token, chat_id,
        f"{len(universe)} hisse taranıyor, bu birkaç dakika sürebilir...",
    )
    adaylar = find_gunici_firsatlari(universe)
    send_telegram_message(token, chat_id, format_gunici(adaylar))


def handle_haber(token, chat_id):
    try:
        items = fetch_rss_items()
    except Exception as e:
        send_telegram_message(token, chat_id, f"Haberler alınamadı: {e}")
        return
    if not items:
        send_telegram_message(token, chat_id, "Şu an gösterilecek haber yok.")
        return
    # RSS'te en yeni en üstte gelir; eskiden yeniye gönder, en yeni sohbette en altta olsun.
    for it in reversed(items[:HABER_LIMIT]):
        analysis = analyze_impact(it)
        send_telegram_message(token, chat_id, format_haber_message(it, analysis))
        time.sleep(HABER_SEND_DELAY_SEC)


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
            elif text == "/liste":
                print("Komut alindi: /liste")
                try:
                    handle_liste(token, chat_id)
                except Exception as e:
                    print(f"Komut isleme hatasi: {e}")
                    send_telegram_message(token, chat_id, "Sorgu sirasinda bir hata olustu.")
            elif text == "/firsat":
                print("Komut alindi: /firsat")
                try:
                    handle_firsat(token, chat_id)
                except Exception as e:
                    print(f"Komut isleme hatasi: {e}")
                    send_telegram_message(token, chat_id, "Sorgu sirasinda bir hata olustu.")
            elif text == "/gunici":
                print("Komut alindi: /gunici")
                try:
                    handle_gunici(token, chat_id)
                except Exception as e:
                    print(f"Komut isleme hatasi: {e}")
                    send_telegram_message(token, chat_id, "Sorgu sirasinda bir hata olustu.")
            elif text == "/haber":
                print("Komut alindi: /haber")
                try:
                    handle_haber(token, chat_id)
                except Exception as e:
                    print(f"Komut isleme hatasi: {e}")
                    send_telegram_message(token, chat_id, "Sorgu sirasinda bir hata olustu.")
            elif text == "/start":
                print("Komut alindi: /start")
                send_telegram_message(token, chat_id, WELCOME_TEXT)
            elif text == "/help":
                print("Komut alindi: /help")
                send_telegram_message(token, chat_id, HELP_TEXT)
            elif text.startswith("/"):
                print(f"Bilinmeyen komut: {text}")
                send_telegram_message(token, chat_id, "Bilinmeyen komut. Komutları görmek için /help yaz.")


if __name__ == "__main__":
    main()
