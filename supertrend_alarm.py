#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BIST 30 SUPERTREND AL SİNYALİ ALARMI
=====================================
BIST 30 (XU030) + kullanıcının izleme listesindeki hisseleri tarar, 1
saatlik mum verisinden Supertrend göstergesini (ATR periyodu 10, çarpan
3 - TradingView varsayılanı) hesaplar. Trend, en son KAPANMIŞ mumda
düşüşten yükselişe döndüyse ("AL" sinyali), bunu Telegram üzerinden
bildirir.

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
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "supertrend_alarm_state.json"
TELEGRAM_CONFIG_FILE = BASE_DIR / "telegram_config.json"
WATCHLIST_FILE = BASE_DIR / "watchlist.json"
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
REQUEST_DELAY_SEC = 0.3
REQUEST_TIMEOUT_SEC = 20

# zaman dilimi etiketi -> (Yahoo interval, Yahoo range). "4s" özel: Yahoo'da
# yerel 4 saatlik mum yok, 60 dakikalık mumlardan yeniden örneklenir.
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "60m": 3600, "1d": 86400, "1wk": 604800}
TIMEFRAME_CONFIG = {
    "5dk": ("5m", "5d"),
    "15dk": ("15m", "1mo"),
    "1s": ("60m", "1mo"),
    "4s": ("60m", "3mo"),
    "1g": ("1d", "1y"),
    "1hf": ("1wk", "5y"),
}
TIMEFRAME_LABELS_ORDERED = ["5dk", "15dk", "1s", "4s", "1g", "1hf"]


def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_candles(ticker, interval, range_):
    """Yahoo Finance'ten OHLC mum serisi çeker (kapanmamış son mum dahil).
    Döner: [(datetime_utc, open, high, low, close), ...] artan zamanlı."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.IS"
           f"?range={range_}&interval={interval}")
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
    return candles


def candle_is_closed(candle_dt_utc, interval, now_utc):
    """Verilen mumun kapanıp kapanmadığını belirler. Gün altı (5m/15m/60m)
    mumlarda süre farkına bakar; günlük/haftalık mumlarda borsanın (İstanbul,
    18:00 kapanış) o periyodu bitirip bitirmediğine bakar."""
    seconds = INTERVAL_SECONDS[interval]
    if seconds < 86400:
        return (now_utc - candle_dt_utc).total_seconds() >= seconds

    candle_local = candle_dt_utc.astimezone(ISTANBUL_TZ)
    now_local = now_utc.astimezone(ISTANBUL_TZ)
    if seconds == 86400:
        return candle_local.date() < now_local.date() or now_local.hour >= 18

    candle_week = candle_local.isocalendar()[:2]
    now_week = now_local.isocalendar()[:2]
    if candle_week != now_week:
        return True
    weekday = now_local.weekday()  # Pazartesi=0 ... Cuma=4
    if weekday < 4:
        return False
    if weekday == 4:
        return now_local.hour >= 18
    return True


def get_closed_candles(ticker, interval, range_):
    """Sadece KAPANMIŞ mumları döner (hâlâ oluşmakta olan son mum atılır)."""
    candles = fetch_candles(ticker, interval, range_)
    if not candles:
        return []
    now = datetime.now(timezone.utc)
    if not candle_is_closed(candles[-1][0], interval, now):
        candles = candles[:-1]
    return candles


def resample_ohlc(candles, group_size):
    """Ardışık mumları, her borsa günü içinde group_size'lık gruplara
    ayırıp birleştirir (örn. 4 saatlik mum için 60 dakikalık mumlardan).
    Günün son grubu group_size'dan az mum içerse bile (oluşmakta olan
    güncel mum olarak) dahil edilir - aksi halde günün en son verisi
    (bugünkü kapanış dahil) tamamen atılmış olur."""
    by_day = defaultdict(list)
    for c in candles:
        by_day[c[0].astimezone(ISTANBUL_TZ).date()].append(c)

    grouped = []
    for day in sorted(by_day):
        day_candles = sorted(by_day[day], key=lambda c: c[0])
        for i in range(0, len(day_candles), group_size):
            chunk = day_candles[i:i + group_size]
            grouped.append((
                chunk[-1][0],
                chunk[0][1],
                max(x[2] for x in chunk),
                min(x[3] for x in chunk),
                chunk[-1][4],
            ))
    return grouped


def get_timeframe_candles(ticker, label):
    """TIMEFRAME_CONFIG'teki bir zaman dilimi etiketi için kapanmış mum
    serisini döner (4s için 60 dakikalık mumlardan yeniden örneklenir)."""
    if label == "4s":
        hourly = get_closed_candles(ticker, "60m", TIMEFRAME_CONFIG["4s"][1])
        return resample_ohlc(hourly, 4)
    interval, range_ = TIMEFRAME_CONFIG[label]
    return get_closed_candles(ticker, interval, range_)


def get_timeframe_status(ticker, label):
    """Bir hissenin verilen zaman diliminde en son kapanmış mumdaki anlık
    Supertrend durumu. Yetersiz veri varsa None döner."""
    candles = get_timeframe_candles(ticker, label)
    st = compute_supertrend(candles)
    if not st:
        return None
    dt, close, st_val, direction = st[-1]
    return {
        "ticker": ticker,
        "zaman_dilimi": label,
        "mum_zamani": dt.astimezone(ISTANBUL_TZ),
        "kapanis": close,
        "supertrend": st_val,
        "yon": direction,
        "mesafe_pct": (close - st_val) / st_val * 100.0,
    }


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


def count_buy_signals(st_series):
    """st_series: compute_supertrend() çıktısı. Serinin tamamında yönün
    -1'den +1'e döndüğü (AL sinyali) an sayısını döner."""
    return sum(
        1 for i in range(1, len(st_series))
        if st_series[i - 1][3] == -1 and st_series[i][3] == 1
    )


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def load_watchlist():
    if WATCHLIST_FILE.exists():
        try:
            return sorted(set(json.loads(WATCHLIST_FILE.read_text())))
        except Exception:
            return []
    return []


def save_watchlist(tickers):
    WATCHLIST_FILE.write_text(json.dumps(sorted(set(tickers)), ensure_ascii=False, indent=2))


def get_scan_universe():
    """AL sinyali taramasında ve genel /durum sorgusunda kullanılacak
    hisse listesi: BIST 30 + kullanıcının izleme listesi."""
    return sorted(set(BIST30_TICKERS) | set(load_watchlist()))


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


def next_run_no(state):
    """Her calistirmada 1 artan, kalici bir tarama numarasi (state icinde
    '_tarama_no' anahtarinda saklanir - hisse kodu olmadigi icin cakismaz)."""
    run_no = state.get("_tarama_no", 0) + 1
    state["_tarama_no"] = run_no
    return run_no


def format_signal_message(signals, run_no):
    now_str = datetime.now(ISTANBUL_TZ).strftime("%Y-%m-%d %H:%M")
    lines = [f"<b>📈 BIST Supertrend AL Sinyali (1S)</b>", f"Tarama #{run_no} — {now_str}", ""]
    for s in signals:
        lines.append(f"• <b>{s['ticker']}</b> — kapanış {s['kapanis']:.2f} (mum: {s['mum_zamani_str']})")
    lines.append("")
    lines.append("Yatırım tavsiyesi değildir.")
    return "\n".join(lines)


def main():
    print("BIST 30 Supertrend (1S) taramasi basliyor...\n")
    state = load_state()
    run_no = next_run_no(state)
    print(f"Tarama #{run_no}\n")
    token, chat_id = load_telegram_config()
    if not token or not chat_id:
        print("UYARI: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID bulunamadi "
              "(ortam degiskeni ya da telegram_config.json). Sinyaller sadece "
              "konsola yazilacak, Telegram'a gonderilmeyecek.\n")

    universe = get_scan_universe()
    interval, range_ = TIMEFRAME_CONFIG["1s"]
    new_signals = []
    for i, ticker in enumerate(universe, 1):
        candles = get_closed_candles(ticker, interval, range_)
        time.sleep(REQUEST_DELAY_SEC)
        st_series = compute_supertrend(candles)
        signal = detect_buy_signal(st_series)

        if signal:
            signal_key = signal["mum_zamani"].isoformat()
            already_sent = state.get(ticker) == signal_key
            status = "AL SINYALI" + (" (zaten bildirildi)" if already_sent else " (YENI)")
            print(f"[{i}/{len(universe)}] {ticker}: {status} - "
                  f"kapanis {signal['kapanis']:.2f}, supertrend {signal['supertrend']:.2f}")
            if not already_sent:
                signal["ticker"] = ticker
                signal["mum_zamani_str"] = signal["mum_zamani"].astimezone(ISTANBUL_TZ).strftime("%Y-%m-%d %H:%M")
                new_signals.append(signal)
                state[ticker] = signal_key
        else:
            print(f"[{i}/{len(universe)}] {ticker}: -")

    save_state(state)

    print("\n=== SONUC ===")
    if not new_signals:
        print("Yeni supertrend AL sinyali yok.")
        return []

    print(f"{len(new_signals)} yeni AL sinyali bulundu:")
    for s in new_signals:
        print(f"  {s['ticker']} - kapanis {s['kapanis']:.2f}")

    if token and chat_id:
        message = format_signal_message(new_signals, run_no)
        ok = send_telegram_message(token, chat_id, message)
        print("Telegram mesaji gonderildi." if ok else "Telegram mesaji GONDERILEMEDI.")
    else:
        print("Telegram ayari olmadigi icin mesaj gonderilmedi (yukaridaki UYARI'ya bakin).")

    return new_signals


if __name__ == "__main__":
    main()
