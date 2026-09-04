#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BIST 30 SUPERTREND AL SİNYALİ ALARMI
=====================================
BIST 30 (XU030) hisselerini tarar, 1 saatlik mum verisinden Supertrend
göstergesini (ATR periyodu 10, çarpan 3 - TradingView varsayılanı) hesaplar.
Trend, en son KAPANMIŞ mumda düşüşten yükselişe döndüyse ("AL" sinyali),
bunu Telegram üzerinden bildirir.

Bağımsız çalışır, hiçbir yerel dosyaya bağımlı değildir. Gün içinde
(BIST işlem saatleri: 10:00-18:00, hafta içi) periyodik olarak - örn.
cron/launchd ile saatte bir - çalıştırılmak üzere tasarlanmıştır. Aynı
mumun sinyalini bir daha göndermez (bkz. supertrend_alarm_state.json).

KULLANIM
--------
    python3 supertrend_alarm.py

TELEGRAM AYARI
---------------
Ortam değişkenleri:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
ya da bu ikisini içeren yerel bir telegram_config.json dosyası
(bkz. telegram_config.json.example). Telegram ayarı yoksa sinyaller
sadece konsola yazılır, hata vermez.

Yatırım tavsiyesi değildir.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "supertrend_alarm_state.json"
TELEGRAM_CONFIG_FILE = BASE_DIR / "telegram_config.json"
ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")

# BIST 30 (XU030) bileşenleri - infoyatirim.com ve getmidas.com üzerinden
# çapraz doğrulandı (2026-09-04). Endeks bileşenleri Borsa İstanbul
# tarafından 3 ayda bir yeniden belirlenir, gerekirse güncelle.
BIST30_TICKERS = sorted([
    "AEFES", "AKBNK", "ASELS", "ASTOR", "BIMAS", "DSTKF", "EKGYO", "ENKAI",
    "EREGL", "FROTO", "GARAN", "GUBRF", "ISCTR", "KCHOL", "KRDMD", "MGROS",
    "PETKM", "PGSUS", "SAHOL", "SASA", "SISE", "TAVHL", "TCELL", "THYAO",
    "TOASO", "TRALT", "TTKOM", "TUPRS", "VAKBN", "YKBNK",
])

ATR_PERIOD = 10
ATR_MULTIPLIER = 3.0
LOOKBACK_RANGE = "1mo"     # supertrend'in ısınması (ATR periyodu) için yeterli mum
REQUEST_DELAY_SEC = 0.3
REQUEST_TIMEOUT_SEC = 20


def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_hourly_ohlc(ticker):
    """Yahoo Finance'ten 1 saatlik OHLC serisi çeker.
    Döner: [(datetime_utc, open, high, low, close), ...] artan zamanlı,
    son (hâlâ oluşmakta olan) kapanmamış mum atılır."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.IS"
           f"?range={LOOKBACK_RANGE}&interval=60m")
    try:
        payload = http_get_json(url)
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except Exception:
        return []

    candles = []
    for i, ts in enumerate(timestamps):
        o, h, l, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
        if None in (o, h, l, c):
            continue
        candles.append((datetime.fromtimestamp(ts, tz=timezone.utc), o, h, l, c))
    candles.sort(key=lambda x: x[0])

    # son mum hâlâ açıksa (kapanmamışsa) at - sadece kapanmış mumlarla çalış
    now = datetime.now(timezone.utc)
    if candles and (now - candles[-1][0]).total_seconds() < 3600:
        candles = candles[:-1]
    return candles


def compute_supertrend(candles, period=ATR_PERIOD, multiplier=ATR_MULTIPLIER):
    """candles: [(dt, o, h, l, c), ...] artan zamanlı.
    Döner: [(dt, close, supertrend_değer, yön), ...] yön: +1 yükseliş, -1 düşüş.
    ATR, Wilder'in RMA yöntemiyle hesaplanır (TradingView varsayılanı)."""
    n = len(candles)
    if n < period + 2:
        return []

    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    closes = [c[4] for c in candles]

    trs = []
    for i in range(n):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))

    atr = [None] * n
    atr[period - 1] = sum(trs[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + trs[i]) / period

    final_upper = [None] * n
    final_lower = [None] * n
    supertrend = [None] * n
    direction = [None] * n

    for i in range(period - 1, n):
        mid = (highs[i] + lows[i]) / 2.0
        basic_upper = mid + multiplier * atr[i]
        basic_lower = mid - multiplier * atr[i]

        if i == period - 1:
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower
            direction[i] = -1
            supertrend[i] = final_upper[i]
            continue

        prev_final_upper = final_upper[i - 1]
        prev_final_lower = final_lower[i - 1]
        prev_close = closes[i - 1]

        final_upper[i] = (basic_upper if (basic_upper < prev_final_upper or prev_close > prev_final_upper)
                           else prev_final_upper)
        final_lower[i] = (basic_lower if (basic_lower > prev_final_lower or prev_close < prev_final_lower)
                           else prev_final_lower)

        prev_direction = direction[i - 1]
        if prev_direction == -1:
            direction[i] = 1 if closes[i] > final_upper[i] else -1
        else:
            direction[i] = -1 if closes[i] < final_lower[i] else 1

        supertrend[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    return [
        (candles[i][0], closes[i], supertrend[i], direction[i])
        for i in range(period - 1, n)
    ]


def detect_buy_signal(st_series):
    """st_series: compute_supertrend() çıktısı. Son kapanan mumda yön
    -1'den +1'e döndüyse sinyal döner, yoksa None."""
    if len(st_series) < 2:
        return None
    _, _, _, prev_dir = st_series[-2]
    last_dt, last_close, last_st, last_dir = st_series[-1]
    if prev_dir == -1 and last_dir == 1:
        return {"mum_zamani": last_dt, "kapanis": last_close, "supertrend": last_st}
    return None


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def load_telegram_config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return token, chat_id
    if TELEGRAM_CONFIG_FILE.exists():
        try:
            cfg = json.loads(TELEGRAM_CONFIG_FILE.read_text())
            return cfg.get("bot_token"), cfg.get("chat_id")
        except Exception:
            pass
    return None, None


def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        print(f"  Telegram hatasi: {e.code} {e.read().decode('utf-8', 'ignore')}")
        return False
    except Exception as e:
        print(f"  Telegram hatasi: {e}")
        return False


def format_signal_message(signals):
    lines = ["<b>📈 BIST 30 - Supertrend AL Sinyali (1S)</b>", ""]
    for s in signals:
        lines.append(f"• <b>{s['ticker']}</b> — kapanış {s['kapanis']:.2f} (mum: {s['mum_zamani_str']})")
    lines.append("")
    lines.append("Yatırım tavsiyesi değildir.")
    return "\n".join(lines)


def main():
    print("BIST 30 Supertrend (1S) taramasi basliyor...\n")
    state = load_state()
    token, chat_id = load_telegram_config()
    if not token or not chat_id:
        print("UYARI: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID bulunamadi "
              "(ortam degiskeni ya da telegram_config.json). Sinyaller sadece "
              "konsola yazilacak, Telegram'a gonderilmeyecek.\n")

    new_signals = []
    for i, ticker in enumerate(BIST30_TICKERS, 1):
        candles = fetch_hourly_ohlc(ticker)
        time.sleep(REQUEST_DELAY_SEC)
        st_series = compute_supertrend(candles)
        signal = detect_buy_signal(st_series)

        if signal:
            signal_key = signal["mum_zamani"].isoformat()
            already_sent = state.get(ticker) == signal_key
            status = "AL SINYALI" + (" (zaten bildirildi)" if already_sent else " (YENI)")
            print(f"[{i}/{len(BIST30_TICKERS)}] {ticker}: {status} - "
                  f"kapanis {signal['kapanis']:.2f}, supertrend {signal['supertrend']:.2f}")
            if not already_sent:
                signal["ticker"] = ticker
                signal["mum_zamani_str"] = signal["mum_zamani"].astimezone(ISTANBUL_TZ).strftime("%Y-%m-%d %H:%M")
                new_signals.append(signal)
                state[ticker] = signal_key
        else:
            print(f"[{i}/{len(BIST30_TICKERS)}] {ticker}: -")

    save_state(state)

    print("\n=== SONUC ===")
    if not new_signals:
        print("Yeni supertrend AL sinyali yok.")
        return []

    print(f"{len(new_signals)} yeni AL sinyali bulundu:")
    for s in new_signals:
        print(f"  {s['ticker']} - kapanis {s['kapanis']:.2f}")

    if token and chat_id:
        message = format_signal_message(new_signals)
        ok = send_telegram_message(token, chat_id, message)
        print("Telegram mesaji gonderildi." if ok else "Telegram mesaji GONDERILEMEDI.")
    else:
        print("Telegram ayari olmadigi icin mesaj gonderilmedi (yukaridaki UYARI'ya bakin).")

    return new_signals


if __name__ == "__main__":
    main()
