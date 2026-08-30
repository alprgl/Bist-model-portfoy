#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BIST100 DÜŞEN TREND KIRILIMI ALARMI
=====================================
BIST100'deki (piyasa değerine göre otomatik seçilen ilk 100) hisseleri tarar,
saatlik fiyat verisinden 4 saatlik mumlar oluşturur ve her biri için son
~20 mumluk (BIST günde ~2-3 mum ürettiği için kabaca ~1.5 hafta) bir lineer
regresyon (trend çizgisi) uydurur. Trend aşağı yönlüyse (eğim negatif) VE en
son mumun kapanışı bu çizginin üzerine ilk kez çıktıysa, bunu bir "düşen
trend kırılımı" sinyali olarak işaretler.

Bu, gerçek bir "trend çizgisi kırılımı" grafik formasyonunun basitleştirilmiş,
istatistiksel bir yaklaşığıdır (gerçek zirve noktalarına çizilen bir çizgi
değil, regresyon çizgisidir). Yatırım tavsiyesi değildir.

Bağımsız çalışır — hisse listesini bilancoveri.com'dan piyasa değerine göre
kendisi seçer, hiçbir yerel dosyaya bağımlı değildir (zamanlanmış görev
içinde herhangi bir ortamda çalışabilir).

KULLANIM
--------
    python3 trend_alarmi.py
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

API_BASE = "https://bilancoveri.com/api/v1"
BULK_URL = f"{API_BASE}/sirketler.json"
TOP_N = 100

LOOKBACK_CANDLES = 20       # trend çizgisi için kaç 4 saatlik mum kullanılacak (~1.5 hafta)
MIN_DOWNTREND_PCT = 2.0     # çizginin basindan sonuna en az bu kadar % düşmüş olmali (gürültüyü ele)
REQUEST_DELAY_SEC = 0.3
REQUEST_TIMEOUT_SEC = 20


def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "trend-alarmi/1.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_top100_tickers():
    payload = http_get_json(BULK_URL)
    companies = payload.get("companies", [])
    companies.sort(key=lambda c: c.get("market_cap_mn_try") or 0, reverse=True)
    top = companies[:TOP_N]
    return {c["ticker"]: c for c in top}


def fetch_hourly_series(ticker, range_str="60d"):
    """Yahoo Finance'ten saatlik kapanis fiyati serisi ceker: [(datetime, close), ...] artan zamanli."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.IS?range={range_str}&interval=60m"
    try:
        payload = http_get_json(url, headers={"User-Agent": "Mozilla/5.0"})
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except Exception:
        return []
    series = [
        (datetime.utcfromtimestamp(ts), c)
        for ts, c in zip(timestamps, closes) if c is not None
    ]
    series.sort(key=lambda x: x[0])
    return series


def resample_to_4h(hourly_series):
    """Saatlik mumlari, gun sinirini asmadan 4'erli gruplara toplayip her grubun SON
    kapanisini '4 saatlik mum' olarak dondurur: [(datetime, close), ...] artan zamanli.
    (BIST gunde ~9 saatlik mum urettigi icin gunde ~2-3 '4 saatlik' mum olusur.)"""
    by_day = {}
    for ts, close in hourly_series:
        by_day.setdefault(ts.date(), []).append((ts, close))

    candles = []
    for day in sorted(by_day):
        day_bars = by_day[day]
        for i in range(0, len(day_bars), 4):
            chunk = day_bars[i:i + 4]
            candles.append(chunk[-1])  # grubun son kapanisi
    return candles


def fetch_price_series(ticker):
    """4 saatlik mum kapanis serisini doner: [(datetime, close), ...] artan zamanli."""
    hourly = fetch_hourly_series(ticker)
    return resample_to_4h(hourly)


def linreg(ys):
    """Basit lineer regresyon: ys = [y0, y1, ...] (x = 0,1,2,...). (egim, kesisim) doner."""
    n = len(ys)
    xs = list(range(n))
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    num = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
    den = sum((xs[i] - x_mean) ** 2 for i in range(n))
    if den == 0:
        return 0.0, y_mean
    slope = num / den
    intercept = y_mean - slope * x_mean
    return slope, intercept


def detect_breakout(series):
    """series: [(date, close), ...] artan tarihli, en az LOOKBACK_CANDLES+2 nokta gerekir.
    Doner: dict veya None (kirilim yoksa)."""
    if len(series) < LOOKBACK_CANDLES + 2:
        return None

    window = series[-(LOOKBACK_CANDLES + 1):-1]   # bugun haric son LOOKBACK_CANDLES gun
    today_date, today_close = series[-1]
    yesterday_date, yesterday_close = series[-2]

    closes = [c for _, c in window]
    slope, intercept = linreg(closes)

    if slope >= 0:
        return None  # dusen trend degil

    trend_start = intercept
    trend_end = slope * (LOOKBACK_CANDLES - 1) + intercept
    if trend_start <= 0:
        return None
    downtrend_pct = (trend_start - trend_end) / trend_start * 100.0
    if downtrend_pct < MIN_DOWNTREND_PCT:
        return None

    trend_value_yesterday = slope * (LOOKBACK_CANDLES - 1) + intercept
    trend_value_today = slope * LOOKBACK_CANDLES + intercept

    was_below = yesterday_close <= trend_value_yesterday
    now_above = today_close > trend_value_today

    if was_below and now_above:
        return {
            "trend_egim_pct": downtrend_pct,
            "kirilim_tarihi": today_date.isoformat(),
            "kapanis": today_close,
            "trend_cizgisi_degeri": trend_value_today,
            "asim_pct": (today_close - trend_value_today) / trend_value_today * 100.0,
        }
    return None


def main():
    print("BIST100 hisse listesi cekiliyor (piyasa degerine gore ilk 100)...")
    companies = fetch_top100_tickers()
    print(f"  -> {len(companies)} hisse. Trend taramasi basliyor...\n")

    signals = []
    for i, (ticker, c) in enumerate(companies.items(), 1):
        series = fetch_price_series(ticker)
        time.sleep(REQUEST_DELAY_SEC)
        result = detect_breakout(series)
        if result:
            signals.append({"ticker": ticker, "name": c.get("name"), **result})
            print(f"[{i}/{len(companies)}] {ticker}: KIRILIM SINYALI (trend egimi -%{result['trend_egim_pct']:.1f}, "
                  f"kapanis {result['kapanis']:.2f}, cizgi {result['trend_cizgisi_degeri']:.2f})")
        else:
            print(f"[{i}/{len(companies)}] {ticker}: -")

    print("\n=== SONUÇ ===")
    if not signals:
        print("Bugün düşen trend kırılımı sinyali veren hisse yok.")
    else:
        signals.sort(key=lambda s: -s["asim_pct"])
        print(f"{len(signals)} hissede düşen trend kırılımı sinyali:\n")
        for s in signals:
            print(f"  {s['ticker']} ({s['name']}) — kapanış {s['kapanis']:.2f}, "
                  f"trend çizgisini %{s['asim_pct']:.1f} aştı, "
                  f"trend eğimi -%{s['trend_egim_pct']:.1f} ({LOOKBACK_CANDLES} mumluk, 4sa)")

    return signals


if __name__ == "__main__":
    main()
