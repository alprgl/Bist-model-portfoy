#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SUPERTREND ALARM KURALLARININ GERIYE DONUK TESTI (BACKTEST)
===========================================================
Iki giris kuralini AYNI cikis kurali altinda karsilastirir:

  Cikis (her iki strateji icin ayni):
      1 saatlik ("1s") Supertrend yonu -1'e (SAT) dondugunde pozisyon kapanir.

  Strateji A (sade referans):
      1 saatlik Supertrend yonu -1'den +1'e dondugu mumda AL.

  Strateji B (supertrend_alarm.py'deki coklu zaman dilimi filtresi):
      Ayni anda
        * 5dk / 15dk / 1s / 2s zaman dilimlerinin HEPSINDE Supertrend yonu +1,
        * ayni dort zaman diliminin HEPSINDE RSI teyitli (50 < RSI < 70),
        * bu dortten EN AZ BIRINDE yuksek hacim (hacim orani >= 2.0 ve
          ilgili mum yukselisteyse: close > open)
      kosullari saglaniyorsa AL.

LOOK-AHEAD (GELECEGE BAKMA) ONLEMLERI
--------------------------------------
1) Sinyal ancak mum KAPANDIKTAN sonra bilinebilir. Bu yuzden sinyal i'inci
   1 saatlik mumun kapanisinda uretilse de, islem (i+1)'inci mumun ACILIS
   fiyatindan yapilir. Cikis da ayni sekilde bir sonraki mumun acilisindan.
2) Yahoo'nun verdigi zaman damgasi mumun BASLANGIC anidir. Her mum icin
   kapanis ani = baslangic + periyot suresi olarak hesaplanir ve tum zaman
   hizalamasi bu KAPANIS anlari uzerinden yapilir.
3) Coklu zaman dilimi hizalamasi: temel zaman cizgisi 1 saatlik mumlardir.
   i'inci saatlik mumun kapanis ani T ise, diger zaman dilimlerinde
   "kapanis_ani <= T" olan SON mum kullanilir (bisect ile). Boylece
   5dk/15dk/2s tarafinda henuz kapanmamis bir mumun bilgisi kullanilmaz.
   Ozellikle 2s mumu 60dk mumlarindan uretildigi icin, onun kapanis ani da
   gruptaki son 60dk mumunun kapanisi olarak alinir.
4) Supertrend, RSI ve hacim oraninin tamami OZYINELI/NEDENSEL (causal)
   gostergelerdir: j'inci degeri yalnizca 0..j mumlarina bagimlidir. Bu
   nedenle seri bir kez bastan sona hesaplanip gecmis degerleri okumak
   gelecege bakma yaratmaz. Hacim bayragi ise `volume_ratio(candles[:j+1])`
   ile birebir ayni sonucu veren O(n) bir on-hesaplama ile uretilir ve
   kodun basinda kutuphane fonksiyonuna karsi dogrulanir (--dogrula).
5) compute_supertrend() ciktisi girdiden KISADIR (ilk period-1 mum isinma
   icin atlanir). Hizalama, her mumun zaman damgasi tek tek karsilastirilarak
   assert ile dogrulanir.

KOMISYON
--------
Her islemden gidis-donus %0.2 dusulur (giriste %0.1, cikista %0.1).

KULLANIM
--------
    python3 backtest.py               # tam test
    python3 backtest.py --detay       # butun islemleri de listele
    python3 backtest.py --dogrula     # ek ic tutarlilik kontrolleri
    python3 backtest.py --cikis       # girisi sabit tutup CIKIS varyantlarini
                                      # karsilastir (Strateji B + A capraz kontrol)

Onbellek: BACKTEST_CACHE_DIR ortam degiskeni verilirse cekilen mumlar
oraya JSON olarak yazilir/okunur (Yahoo'yu tekrar tekrar yormamak icin).

Yatirim tavsiyesi degildir.
"""

import bisect
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from supertrend_alarm import (
    ATR_PERIOD,
    BIST30_TICKERS,
    ISTANBUL_TZ,
    RSI_PERIOD,
    compute_rsi,
    compute_supertrend,
    get_closed_candles,
    last_traded_candle,
    resample_ohlc,
    rsi_confirms,
    volume_ratio,
)

# ---------------------------------------------------------------- ayarlar --
LOOKBACK_DAYS = 90              # "son 3 ay"
COMMISSION_ONE_WAY = 0.001      # tek yon %0.1 -> gidis-donus %0.2
VOLUME_LOOKBACK = 20
VOLUME_RATIO_MIN = 2.0
SETTLE_BARS = 100               # Supertrend'in baslangic tohumundan (yon=-1)
                                # arinmasi icin ek isinma payi
REQUEST_DELAY_SEC = 0.3

BASE_TF = "1s"
ENTRY_TFS = ["5dk", "15dk", "1s", "2s"]

# Canli bottaki TIMEFRAME_CONFIG kisa araliklar kullanir ("1mo", "5d"); geriye
# donuk test icin ayni interval'lerin mumkun olan EN UZUN gecmisi cekilir.
# (interval, range, 60dk mumlarindan yeniden orneklerken grup buyuklugu)
FETCH_PLAN = {
    "5dk":  ("5m",  "60d", None),   # Yahoo 5dk icin en fazla ~60 gun verir
    "15dk": ("15m", "60d", None),
    "1s":   ("60m", "6mo", None),
    "2s":   ("60m", "6mo", 2),      # 60dk mumlarindan uretilir
}
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "60m": 3600}

CACHE_DIR = os.environ.get("BACKTEST_CACHE_DIR")


# ------------------------------------------------------------- veri cekme --
_fetch_cache = {}


def fetch_raw(ticker, interval, range_):
    """(ticker, interval, range) icin kapanmis mumlar; bellekte (ve istege
    bagli olarak diskte) onbelleklenir."""
    key = (ticker, interval, range_)
    if key in _fetch_cache:
        return _fetch_cache[key]

    path = None
    if CACHE_DIR:
        path = Path(CACHE_DIR) / f"{ticker}_{interval}_{range_}.json"
        if path.exists():
            raw = json.loads(path.read_text())
            candles = [(datetime.fromisoformat(r[0]), r[1], r[2], r[3], r[4], r[5])
                       for r in raw]
            _fetch_cache[key] = candles
            return candles

    candles = get_closed_candles(ticker, interval, range_)
    time.sleep(REQUEST_DELAY_SEC)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([[c[0].isoformat()] + list(c[1:]) for c in candles]))
    _fetch_cache[key] = candles
    return candles


def get_series_candles(ticker, label):
    """Bir zaman dilimi etiketi icin mum serisi (2s, 60dk'dan yeniden
    orneklenir - get_timeframe_candles ile ayni mantik, daha uzun aralikla)."""
    interval, range_, group = FETCH_PLAN[label]
    candles = fetch_raw(ticker, interval, range_)
    if group:
        return resample_ohlc(candles, group)
    return candles


# ------------------------------------------------------------ gostergeler --
def high_volume_flags(candles, lookback=VOLUME_LOOKBACK):
    """j'inci eleman: "j'inci mum kapandiginda yuksek hacim var mi?".
    volume_ratio(candles[:j+1]) + last_traded_candle(candles[:j+1]) ikilisinin
    O(n) esdegeri. Hacmi 0 olan mumlar (BIST kapanis muzayedesi) referans
    olarak alinmaz, ama 20 mumluk ortalamaya - kutuphane fonksiyonunda oldugu
    gibi - dahil edilir."""
    n = len(candles)
    flags = [False] * n
    ratios = [None] * n
    vols = [c[5] for c in candles]
    prefix = [0.0] * (n + 1)
    for i, v in enumerate(vols):
        prefix[i + 1] = prefix[i] + v

    last_traded = None
    for j in range(n):
        if vols[j] > 0:
            last_traded = j
        k = last_traded
        if k is None or k < lookback:
            continue
        avg = (prefix[k] - prefix[k - lookback]) / lookback
        if avg <= 0:
            continue
        ratio = vols[k] / avg
        ratios[j] = ratio
        c = candles[k]
        flags[j] = ratio >= VOLUME_RATIO_MIN and c[4] > c[1]
    return flags, ratios


def wilder_atr(candles, period=ATR_PERIOD):
    """compute_supertrend() icindekiyle BIREBIR ayni ATR (Wilder RMA).
    Doner: mum sayisiyla ayni uzunlukta liste, isinma donemi None.
    i'inci deger yalnizca 0..i mumlarina bagli (nedensel)."""
    n = len(candles)
    atr = [None] * n
    if n < period:
        return atr
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]
    closes = [c[4] for c in candles]
    trs = []
    for i in range(n):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(max(highs[i] - lows[i],
                           abs(highs[i] - closes[i - 1]),
                           abs(lows[i] - closes[i - 1])))
    atr[period - 1] = sum(trs[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + trs[i]) / period
    return atr


class Series:
    """Bir hisse + zaman dilimi icin, her mum indeksinde nokta-zamanli
    (point-in-time) gosterge degerleri."""

    def __init__(self, label, candles):
        self.label = label
        self.candles = candles
        self.n = len(candles)

        interval = FETCH_PLAN[label][0]
        step = INTERVAL_SECONDS[interval]
        # Yahoo zaman damgasi = mumun BASLANGICI. 2s mumunun zaman damgasi
        # gruptaki son 60dk mumunun baslangicidir; dolayisiyla ikisinde de
        # kapanis = damga + 60dk (2s icin) / + periyot (digerleri icin).
        self.close_times = [c[0] + timedelta(seconds=step) for c in candles]
        assert all(self.close_times[i] < self.close_times[i + 1]
                   for i in range(self.n - 1)), f"{label}: zaman damgalari artan degil"

        # Supertrend ciktisi girdiden kisa: ilk ATR_PERIOD-1 mum atlanir.
        st = compute_supertrend(candles)
        self.dirs = [None] * self.n
        self.st_vals = [None] * self.n
        if st:
            offset = self.n - len(st)
            assert offset == ATR_PERIOD - 1, f"{label}: beklenmeyen supertrend kaymasi {offset}"
            for k, (dt, _close, st_val, direction) in enumerate(st):
                idx = offset + k
                # hizalamayi zaman damgasi uzerinden bizzat dogrula
                assert candles[idx][0] == dt, f"{label}: supertrend indeks kaymasi @{idx}"
                self.dirs[idx] = direction
                self.st_vals[idx] = st_val

        self.rsi = compute_rsi(candles)
        assert len(self.rsi) == self.n, f"{label}: RSI uzunlugu mum sayisiyla ayni degil"

        self.atr = wilder_atr(candles, ATR_PERIOD)

        self.hivol, self.vol_ratio = high_volume_flags(candles)

        # Gostergelerin tamaminin gecerli oldugu ilk indeks + tohum arinma payi
        self.first_valid = max(ATR_PERIOD - 1, RSI_PERIOD, VOLUME_LOOKBACK) + SETTLE_BARS

    def index_asof(self, t):
        """Kapanis ani <= t olan SON mumun indeksi; yoksa None."""
        i = bisect.bisect_right(self.close_times, t) - 1
        return i if i >= 0 else None

    def ready_from(self):
        """Bu zaman diliminin kullanilabilir hale geldigi an."""
        if self.n <= self.first_valid:
            return None
        return self.close_times[self.first_valid]


def build_series(ticker):
    out = {}
    for label in ENTRY_TFS:
        candles = get_series_candles(ticker, label)
        if len(candles) < 60:
            return None
        out[label] = Series(label, candles)
    return out


# ------------------------------------------------------------- simulasyon --
def entry_signal_a(base, i):
    """1s Supertrend yonu -1'den +1'e dondu mu (i'inci mumun kapanisinda)."""
    if i < 1:
        return False
    return base.dirs[i - 1] == -1 and base.dirs[i] == 1


def entry_signal_b(series, t):
    """t aninda (bir 1s mumunun kapanis ani) coklu zaman dilimi filtresi."""
    any_hivol = False
    for label in ENTRY_TFS:
        s = series[label]
        j = s.index_asof(t)
        if j is None or j < s.first_valid:
            return False
        if s.dirs[j] != 1:
            return False
        if not rsi_confirms(s.rsi[j], 1):
            return False
        if s.hivol[j]:
            any_hivol = True
    return any_hivol


# ------------------------------------------------------------ cikis kurali --
# Bir cikis kurali sozlugu. Bos sozluk = orijinal davranis (1s Supertrend SAT).
DEFAULT_EXIT = {
    "st_tf": BASE_TF,   # Supertrend cikisini hangi zaman dilimi verir
    "stop_pct": None,   # girise gore sabit yuzde zarar durdur (0.02 = %2)
    "atr_k": None,      # giris - k * ATR(10) zarar durdur
    "tp_pct": None,     # kar al yuzdesi
    "trail_pct": None,  # en yuksek KAPANISTAN yuzde geri cekilme (takip eden)
    "kapanis_ile": False,  # True: seviye kontrolu sadece mum KAPANISIYLA yapilir
                           # ve cikis bir sonraki mumun acilisindan olur
                           # False: seviye mum ICINDE (low/high) tetiklenir
}
_PRICE_KEYS = ("stop_pct", "atr_k", "tp_pct", "trail_pct")


def st_dir_asof(s, t):
    """s zaman dilimindeki, kapanis ani <= t olan son mumun Supertrend yonu."""
    j = s.index_asof(t)
    if j is None or j < s.first_valid:
        return None
    return s.dirs[j]


def simulate(series, strategy, start_idx, end_idx, exit_rule=None):
    """start_idx..end_idx araligindaki 1s mumlari uzerinde tek pozisyonlu,
    yalnizca ALIS yonunde simulasyon.

    Zaman akisi her mum icin sirayla:
      1) mumun ACILISINDA bir onceki mumun kapanisinda uretilmis bekleyen
         emir (giris/cikis) gerceklestirilir,
      2) mum ICINDE fiyat seviyesi (zarar durdur / kar al / takip eden) veya
         daha kisa zaman dilimi Supertrend cikisi tetiklenmis mi bakilir,
      3) mumun KAPANISINDA portfoy piyasaya isaretlenir,
      4) mumun KAPANISINDA yeni sinyal uretilir ve BIR SONRAKI mumun
         acilisi icin siparis edilir.
    Boylece hicbir karar, karar aninda bilinemeyecek bir fiyata dayanmaz.

    YENIDEN GIRIS KILIDI: fiyat seviyesiyle (stop/kar al/takip eden) cikildiysa
    pozisyon 1s Supertrend bir kez SAT'a donmeden yeniden acilmaz. Strateji B
    bir DURUM kuralidir (kesisme degil); bu kilit olmasa stop'tan cikilan mumun
    kapanisinda ayni kosul hala gecerli olur ve pozisyon aninda geri acilirdi -
    stop hicbir ise yaramaz, sadece komisyon yakardi. Referans kuralda her cikis
    zaten 1s SAT ile olustugu icin kilit HICBIR SEY DEGISTIRMEZ (varsayilan
    calistirma birebir eskisi gibi).

    Doner: (trades, equity) - equity, base zaman cizgisiyle ayni uzunlukta
    (araligin disinda None)."""
    rule = dict(DEFAULT_EXIT)
    if exit_rule:
        rule.update(exit_rule)
    base = series[BASE_TF]
    st_tf = rule["st_tf"]
    st_s = series[st_tf]
    # 5dk/15dk cikisi saat ICINDE gerceklesir (sinyal mumu kapanir, bir sonraki
    # 5dk/15dk mumunun acilisindan cikilir). 1s/2s saatlik izgarayla hizalidir.
    intrabar_st = st_tf in ("5dk", "15dk")
    st_reason = "supertrend_sat" if st_tf == BASE_TF else f"supertrend_sat_{st_tf}"
    price_exit = any(rule[k] is not None for k in _PRICE_KEYS)
    kapanis_ile = rule["kapanis_ile"]

    trades = []
    equity = [None] * base.n

    cash = 1.0          # pozisyon disindayken sermaye carpani
    in_pos = False
    entry_price = None
    entry_i = None
    stop_level = None
    tp_level = None
    peak_close = None
    pending_entry = False
    pending_exit = None     # cikis sebebi ya da None
    armed = True            # fiyat-stop sonrasi yeniden giris kilidi

    def open_position(i):
        nonlocal entry_i, entry_price, in_pos, stop_level, tp_level, peak_close
        entry_i = i
        entry_price = base.candles[i][1]
        in_pos = True
        peak_close = entry_price
        stop_level = None
        tp_level = None
        if rule["stop_pct"] is not None:
            stop_level = entry_price * (1 - rule["stop_pct"])
        if rule["atr_k"] is not None:
            # ATR, sinyalin uretildigi (i-1) mumun kapanisinda bilinir
            a = base.atr[i - 1] if i > 0 else None
            if a:
                stop_level = entry_price - rule["atr_k"] * a
        if rule["tp_pct"] is not None:
            tp_level = entry_price * (1 + rule["tp_pct"])

    def current_stop(i):
        """Bu mum boyunca gecerli olan zarar durdur seviyesi."""
        if rule["trail_pct"] is not None:
            # peak_close yalnizca i'den ONCEKI mumlarin kapanislarini icerir
            return peak_close * (1 - rule["trail_pct"])
        return stop_level

    for i in range(start_idx, end_idx + 1):
        o, hi_p, lo_p, cl = (base.candles[i][1], base.candles[i][2],
                             base.candles[i][3], base.candles[i][4])

        # 1) ACILIS: bekleyen emirler
        if pending_exit:
            cash = close_trade(trades, cash, base, entry_i, i,
                               entry_price, o, pending_exit, False)
            if pending_exit not in (st_reason,):
                armed = False
            in_pos = False
            entry_price = entry_i = None
            pending_exit = None
        if pending_entry:
            open_position(i)
            pending_entry = False

        # 2) MUM ICI cikislar
        if in_pos and price_exit and not kapanis_ile:
            lvl = current_stop(i)
            if lvl is not None and lo_p <= lvl:
                # Gedikli acilista emir acilistan gerceklesir (daha kotu fiyat)
                fill = min(lvl, o)
                cash = close_trade(trades, cash, base, entry_i, i,
                                   entry_price, fill, "zarar_durdur", False)
                in_pos = False
                armed = False
                entry_price = entry_i = None
            elif tp_level is not None and hi_p >= tp_level:
                fill = max(tp_level, o)   # yukari gedikte limit emri acilistan doner
                cash = close_trade(trades, cash, base, entry_i, i,
                                   entry_price, fill, "kar_al", False)
                in_pos = False
                armed = False
                entry_price = entry_i = None

        if in_pos and intrabar_st:
            lo_j = st_s.index_asof(base.candles[i][0])
            hi_j = st_s.index_asof(base.close_times[i])
            if hi_j is not None:
                j0 = 0 if lo_j is None else lo_j + 1
                for j in range(max(j0, st_s.first_valid), hi_j + 1):
                    if st_s.dirs[j] == -1:
                        nxt = j + 1
                        fill = (st_s.candles[nxt][1] if nxt < st_s.n
                                else st_s.candles[j][4])
                        cash = close_trade(trades, cash, base, entry_i, i,
                                           entry_price, fill, st_reason, False)
                        in_pos = False
                        entry_price = entry_i = None
                        break

        # 3) KAPANIS: piyasaya isaretle (cikis komisyonu dahil tasfiye degeri)
        if in_pos:
            equity[i] = cash * (cl / entry_price) * (1 - COMMISSION_ONE_WAY) ** 2
        else:
            equity[i] = cash

        # 4) KAPANIS: yeni sinyal -> bir sonraki mumun acilisinda uygulanacak
        if base.dirs[i] == -1:
            armed = True
        if i < end_idx:
            if in_pos:
                if kapanis_ile and price_exit:
                    lvl = current_stop(i)
                    if lvl is not None and cl <= lvl:
                        pending_exit = "zarar_durdur"
                    elif tp_level is not None and cl >= tp_level:
                        pending_exit = "kar_al"
                if pending_exit is None and not intrabar_st:
                    d = (base.dirs[i] if st_tf == BASE_TF
                         else st_dir_asof(st_s, base.close_times[i]))
                    if d == -1:
                        pending_exit = st_reason
            else:
                fire = (entry_signal_a(base, i) if strategy == "A"
                        else entry_signal_b(series, base.close_times[i]))
                if fire and armed:
                    pending_entry = True
        if in_pos:
            peak_close = max(peak_close, cl)

    # veri sonunda hala acik pozisyon varsa son kapanistan degerle
    if in_pos:
        cash = close_trade(trades, cash, base, entry_i, end_idx,
                           entry_price, base.candles[end_idx][4], "veri_sonu", True)
        equity[end_idx] = cash

    return trades, equity


def close_trade(trades, cash, base, entry_i, exit_i, entry_price, exit_price,
                reason, still_open):
    gross = exit_price / entry_price
    net = gross * (1 - COMMISSION_ONE_WAY) ** 2
    trades.append({
        "giris_dt": base.candles[entry_i][0],
        "cikis_dt": base.candles[exit_i][0],
        "giris": entry_price,
        "cikis": exit_price,
        "brut_getiri": gross - 1.0,
        "net_getiri": net - 1.0,
        "bar": exit_i - entry_i,
        "sebep": reason,
        "acik_kapatildi": still_open,
    })
    return cash * net


def buy_and_hold(series, start_idx, end_idx):
    """Pencerenin ilk mumunun acilisindan al, son mumun kapanisindan sat."""
    base = series[BASE_TF]
    entry_price = base.candles[start_idx][1]
    equity = [None] * base.n
    for i in range(start_idx, end_idx + 1):
        equity[i] = (base.candles[i][4] / entry_price) * (1 - COMMISSION_ONE_WAY) ** 2
    net = equity[end_idx]
    trade = {
        "giris_dt": base.candles[start_idx][0],
        "cikis_dt": base.candles[end_idx][0],
        "giris": entry_price,
        "cikis": base.candles[end_idx][4],
        "brut_getiri": base.candles[end_idx][4] / entry_price - 1.0,
        "net_getiri": net - 1.0,
        "bar": end_idx - start_idx,
        "sebep": "al_ve_tut",
        "acik_kapatildi": True,
    }
    return [trade], equity


# ---------------------------------------------------------------- metrikler --
def trade_stats(trades):
    n = len(trades)
    if n == 0:
        return {"islem": 0}
    rets = [t["net_getiri"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gain = sum(wins)
    loss = -sum(losses)
    return {
        "islem": n,
        "kazanan": len(wins),
        "kaybeden": len(losses),
        "kazanma_orani": len(wins) / n,
        "ort_kazanc": (gain / len(wins)) if wins else 0.0,
        "ort_kayip": (-loss / len(losses)) if losses else 0.0,
        "ort_islem": sum(rets) / n,
        "en_iyi": max(rets),
        "en_kotu": min(rets),
        "kar_faktoru": (gain / loss) if loss > 0 else float("inf"),
        "ort_bar": sum(t["bar"] for t in trades) / n,
    }


def portfolio_curve(equity_by_ticker, grid):
    """Her hisseye esit agirlikta (1/N) baslangic sermayesi; her dilim kendi
    icinde birlesik getiriyle ilerler, aralarinda yeniden dengeleme yok.
    Ortak zaman izgarasinda ileri-doldurma (forward fill) ile toplanir."""
    n_t = len(equity_by_ticker)
    if n_t == 0:
        return []
    filled = []
    for eq, times in equity_by_ticker:
        by_time = dict(zip(times, eq))
        cur = 1.0
        row = []
        for t in grid:
            if t in by_time and by_time[t] is not None:
                cur = by_time[t]
            row.append(cur)
        filled.append(row)
    return [sum(col) / n_t for col in zip(*filled)]


def t_stat(rets):
    """Islem basina ortalama getirinin sifirdan farkli olup olmadigina dair
    kaba bir t istatistigi (normallik varsayimi zayif, sadece fikir verir)."""
    n = len(rets)
    if n < 2:
        return 0.0, 0.0
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    sd = var ** 0.5
    if sd == 0:
        return mean, 0.0
    return mean, mean / (sd / n ** 0.5)


def concentration(per_ticker_rets):
    """Toplam kar/zararin ne kadari tek bir hisseden geliyor?"""
    items = sorted(per_ticker_rets.items(), key=lambda kv: kv[1], reverse=True)
    toplam = sum(per_ticker_rets.values())
    en_iyi = items[0] if items else (None, 0.0)
    return en_iyi, toplam


def max_drawdown(curve):
    peak = -1e18
    mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


# ------------------------------------------------------------ dogrulamalar --
def verify_volume_flags(series):
    """high_volume_flags()'in kutuphanedeki volume_ratio/last_traded_candle
    ikilisiyle birebir ayni sonucu urettigini rastgele indekslerde dogrula."""
    rng = random.Random(42)
    checked = 0
    for label, s in series.items():
        idxs = rng.sample(range(s.first_valid, s.n), min(40, s.n - s.first_valid))
        for j in idxs:
            sub = s.candles[:j + 1]
            ratio = volume_ratio(sub, VOLUME_LOOKBACK)
            ref = last_traded_candle(sub)
            expect = bool(ratio and ref and ratio >= VOLUME_RATIO_MIN and ref[4] > ref[1])
            assert expect == s.hivol[j], f"{label} @{j}: hacim bayragi uyusmuyor"
            checked += 1
    return checked


# ------------------------------------------------------------- evren kurma --
def load_universe(dogrula=False, ilerleme=None):
    """BIST30'un tamamini yukler ve her hisse icin test penceresini belirler.

    Doner: (evren, grid, pencere_bilgi, basarisiz, dogrulanan)
      evren = [(ticker, series, start_idx, end_idx), ...]
    ilerleme(n, toplam, ticker, series, start_idx, end_idx) her basarili
    hissede cagrilir (ekrana yazdirmak icin)."""
    now = datetime.now(timezone.utc)
    window_floor = now - timedelta(days=LOOKBACK_DAYS)

    evren = []
    grid_times = set()
    basarisiz = []
    pencere_bilgi = []
    dogrulanan = 0
    toplam = len(BIST30_TICKERS)

    for n, ticker in enumerate(BIST30_TICKERS, 1):
        try:
            series = build_series(ticker)
        except Exception as exc:  # ag hatasi / bozuk veri
            basarisiz.append((ticker, f"veri hatasi: {exc}"))
            print(f"[{n:2}/{toplam}] {ticker}: ATLANDI ({exc})")
            continue
        if series is None:
            basarisiz.append((ticker, "yetersiz mum"))
            print(f"[{n:2}/{toplam}] {ticker}: ATLANDI (yetersiz mum)")
            continue

        if dogrula:
            dogrulanan += verify_volume_flags(series)

        # Pencere baslangici: butun zaman dilimlerinin gosterge + tohum
        # isinmasini tamamladigi an, ama en erken "son 3 ay" siniri.
        readys = [s.ready_from() for s in series.values()]
        if any(r is None for r in readys):
            basarisiz.append((ticker, "isinma icin yetersiz veri"))
            print(f"[{n:2}/{toplam}] {ticker}: ATLANDI (isinma yetersiz)")
            continue
        start_time = max(max(readys), window_floor)

        base = series[BASE_TF]
        start_idx = None
        for i in range(base.n):
            if base.close_times[i] >= start_time and i >= base.first_valid:
                start_idx = i
                break
        end_idx = base.n - 1
        if start_idx is None or end_idx - start_idx < 30:
            basarisiz.append((ticker, "pencere cok kisa"))
            print(f"[{n:2}/{toplam}] {ticker}: ATLANDI (pencere kisa)")
            continue

        pencere_bilgi.append((ticker, base.candles[start_idx][0],
                              base.candles[end_idx][0], end_idx - start_idx + 1))
        grid_times.update(c[0] for c in base.candles[start_idx:end_idx + 1])
        evren.append((ticker, series, start_idx, end_idx))
        if ilerleme:
            ilerleme(n, toplam, ticker, series, start_idx, end_idx)

    return evren, sorted(grid_times), pencere_bilgi, basarisiz, dogrulanan


def run_strategy(evren, grid, strategy, exit_rule=None):
    """Butun evrende tek bir (giris, cikis) kombinasyonunu kosturur ve
    portfoy duzeyinde ozet metrikleri dondurur."""
    per_ticker = []
    equity_sets = []
    for ticker, series, start_idx, end_idx in evren:
        base = series[BASE_TF]
        trades, eq = simulate(series, strategy, start_idx, end_idx, exit_rule)
        per_ticker.append((ticker, trades))
        equity_sets.append(([eq[i] for i in range(start_idx, end_idx + 1)],
                            [base.candles[i][0] for i in range(start_idx, end_idx + 1)]))

    all_trades = [t for _tic, ts in per_ticker for t in ts]
    st = trade_stats(all_trades)
    curve = portfolio_curve(equity_sets, grid)
    st["toplam_getiri"] = curve[-1] - 1.0 if curve else 0.0
    st["max_dusus"] = max_drawdown(curve) if curve else 0.0
    st["egri"] = curve
    st["hisse_basi"] = dict(per_ticker)
    st["islemler"] = all_trades
    toplam_bar = sum(e - s + 1 for _t, _se, s, e in evren)
    st["kalma_orani"] = (sum(t["bar"] for t in all_trades) / toplam_bar) if toplam_bar else 0.0
    _mean, tval = t_stat([t["net_getiri"] for t in all_trades])
    st["t"] = tval
    # hisse bazinda birlesik getiri
    bileske = {}
    for tic, ts in per_ticker:
        v = 1.0
        for t in ts:
            v *= (1 + t["net_getiri"])
        bileske[tic] = v - 1.0
    st["hisse_getiri"] = bileske
    st["arti_hisse"] = sum(1 for v in bileske.values() if v > 0)
    st["hisse_sayisi"] = len(bileske)
    st["medyan_hisse"] = sorted(bileske.values())[len(bileske) // 2] if bileske else 0.0
    en_iyi = max(bileske.items(), key=lambda kv: kv[1]) if bileske else (None, 0.0)
    st["en_iyi_hisse"] = en_iyi
    kalan = [v for k, v in bileske.items() if k != en_iyi[0]]
    # en iyi hisse cikarilinca portfoyun kaba getirisi (esit agirlikli ortalama)
    st["en_iyi_haric_ort"] = (sum(kalan) / len(kalan)) if kalan else 0.0
    return st


# ------------------------------------------------------- cikis varyantlari --
# Not: 95 islemlik bir ornek uzerinde bu kadar varyant denemek, en iyisinin
# sans eseri one cikma olasiligini ciddi sekilde artirir. Tablo bu yuzden
# TAMAMEN raporlanir.
EXIT_VARIANTS = [
    ("V1  referans 1s ST SAT",        {}),
    ("V2a sabit stop %2",             {"stop_pct": 0.02}),
    ("V2b sabit stop %3",             {"stop_pct": 0.03}),
    ("V2c sabit stop %5",             {"stop_pct": 0.05}),
    ("V3a ATR stop 1.5x",             {"atr_k": 1.5}),
    ("V3b ATR stop 2.5x",             {"atr_k": 2.5}),
    ("V4a kar al +%5",                {"tp_pct": 0.05}),
    ("V4b kar al +%10",               {"tp_pct": 0.10}),
    ("V5a takip eden stop %3",        {"trail_pct": 0.03}),
    ("V5b takip eden stop %6",        {"trail_pct": 0.06}),
    ("V6  hizli cikis 15dk ST",       {"st_tf": "15dk"}),
    ("V7  yavas cikis 2s ST",         {"st_tf": "2s"}),
]

# Yurutme varsayimina duyarlilik kontrolu (aday degil, saglamlik testi):
# ayni seviyeler mum ICINDE degil yalnizca KAPANISTA kontrol edilir ve cikis
# bir sonraki mumun acilisindan yapilir.
ROBUSTNESS_VARIANTS = [
    ("V2b' stop %3 (sadece kapanis)",  {"stop_pct": 0.03, "kapanis_ile": True}),
    ("V5a' takip %3 (sadece kapanis)", {"trail_pct": 0.03, "kapanis_ile": True}),
    ("V4a' kar al +%5 (sadece kap.)",  {"tp_pct": 0.05, "kapanis_ile": True}),
]


def spearman(xs, ys):
    """Iki listenin sira (rank) korelasyonu. Beraberlik yoksa basit formul."""
    n = len(xs)
    if n < 3:
        return 0.0

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        for pos, i in enumerate(order):
            r[i] = pos + 1.0
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den = (sum((rx[i] - mx) ** 2 for i in range(n)) *
           sum((ry[i] - my) ** 2 for i in range(n))) ** 0.5
    return num / den if den else 0.0


def varyant_tablosu(baslik, satirlar):
    print()
    print("=" * 110)
    print(baslik)
    print("=" * 110)
    print(f"{'Varyant':32} {'Islem':>6} {'Kazan%':>7} {'Ort.islem':>10} "
          f"{'Toplam':>9} {'MaksDus':>9} {'KarFakt':>8} {'OrtBar':>7} "
          f"{'Arti hisse':>11} {'t':>6}")
    print("-" * 110)
    for etiket, s in satirlar:
        print(f"{etiket:32} {s['islem']:>6} "
              f"{s.get('kazanma_orani', 0) * 100:>6.1f}% "
              f"{pct(s.get('ort_islem', 0)):>10} "
              f"{pct(s['toplam_getiri']):>9} {pct(s['max_dusus']):>9} "
              f"{s.get('kar_faktoru', 0):>8.2f} {s.get('ort_bar', 0):>7.1f} "
              f"{s['arti_hisse']:>5}/{s['hisse_sayisi']:<5} {s['t']:>6.2f}")


def cikis_karsilastirma():
    """--cikis: girisi SABIT tutup (Strateji B) yalnizca cikis kuralini
    degistiren karsilastirma."""
    print("=" * 110)
    print("CIKIS KURALI VARYANTLARI - giris SABIT (Strateji B)")
    print("=" * 110)
    print(f"Komisyon: gidis-donus %{COMMISSION_ONE_WAY * 200:.1f}   "
          f"Temel zaman dilimi: {BASE_TF}   Donem: son {LOOKBACK_DAYS} gun")
    print()
    print("VARSAYIM: zarar durdur / kar al / takip eden stop seviyeleri mumun")
    print("ICINDE (low/high) tetiklenir; emir tam o seviyeden gerceklesir.")
    print("Fiyat mumun ACILISINDA seviyeyi gecmisse (gedik) daha kotu olan")
    print("acilis fiyati kullanilir. Kayma (slippage) yoktur -> IYIMSER.")
    print("Sonda ayni kurallarin 'sadece kapanis' versiyonu da verilir.")
    print()

    def ilerleme(n, toplam, ticker, *_):
        print(f"[{n:2}/{toplam}] {ticker}: yuklendi")

    evren, grid, pencere_bilgi, basarisiz, _ = load_universe(ilerleme=ilerleme)
    if not evren:
        print("Veri alinamadi.")
        return

    ilk = min(p[1] for p in pencere_bilgi)
    son = max(p[2] for p in pencere_bilgi)
    print()
    print(f"Pencere: {ilk.astimezone(ISTANBUL_TZ):%Y-%m-%d %H:%M} - "
          f"{son.astimezone(ISTANBUL_TZ):%Y-%m-%d %H:%M}, "
          f"{len(pencere_bilgi)} hisse")
    if basarisiz:
        print(f"Atlanan: {', '.join(t for t, _ in basarisiz)}")

    sonuc_b = [(etiket, run_strategy(evren, grid, "B", rule))
               for etiket, rule in EXIT_VARIANTS]
    varyant_tablosu("GIRIS B - TUM CIKIS VARYANTLARI", sonuc_b)

    saglamlik = [(etiket, run_strategy(evren, grid, "B", rule))
                 for etiket, rule in ROBUSTNESS_VARIANTS]
    varyant_tablosu("YURUTME VARSAYIMI DUYARLILIGI (giris B, sadece kapanis kontrolu)",
                    saglamlik)

    # ---- capraz kontrol: TUM varyantlar, Strateji A girisiyle ----
    # Sadece "en iyi"leri capraz kontrol etmek yine bir secim yanliligidir;
    # bu yuzden 12 varyantin TAMAMI A girisiyle de kosturulur.
    referans = dict(sonuc_b)[EXIT_VARIANTS[0][0]]
    sonuc_a = [(etiket, run_strategy(evren, grid, "A", rule))
               for etiket, rule in EXIT_VARIANTS]
    varyant_tablosu("CAPRAZ KONTROL: ayni cikislarin TAMAMI, STRATEJI A girisiyle",
                    sonuc_a)

    print()
    print("=" * 110)
    print("CAPRAZ KONTROL OZETI (referans cikisa gore toplam getiri farki)")
    print("=" * 110)
    a_ref = dict(sonuc_a)[EXIT_VARIANTS[0][0]]
    print(f"{'Varyant':32} {'B: fark':>12} {'A: fark':>12}  {'ayni yonde mi?':>16}")
    print("-" * 110)
    ciftler = []
    for etiket, _rule in EXIT_VARIANTS[1:]:
        db = dict(sonuc_b)[etiket]["toplam_getiri"] - referans["toplam_getiri"]
        da = dict(sonuc_a)[etiket]["toplam_getiri"] - a_ref["toplam_getiri"]
        ayni = "EVET" if (db > 0) == (da > 0) else "HAYIR (supheli)"
        ciftler.append((db, da))
        print(f"{etiket:32} {pct(db):>12} {pct(da):>12}  {ayni:>16}")
    uyum = sum(1 for db, da in ciftler if (db > 0) == (da > 0))
    print(f"\nAyni yonde hareket eden varyant: {uyum}/{len(ciftler)} "
          f"(sirf sansla beklenen ~{len(ciftler) / 2:.0f})")
    print(f"Sira korelasyonu (Spearman, B vs A toplam getiri): "
          f"{spearman([s['toplam_getiri'] for _e, s in sonuc_b], [s['toplam_getiri'] for _e, s in sonuc_a]):.2f}")

    # ---- hisse bazinda tutarlilik ----
    print()
    print("=" * 110)
    print("HISSE BAZINDA TUTARLILIK (giris B) - kazanc kac hisseden geliyor?")
    print("=" * 110)
    print("Not: esit agirlikli portfoyde toplam getiri = hisse getirilerinin")
    print("ortalamasi; 'TUPRS haric' sutunu TUPRS disindaki 29 hissenin ortalamasi.")
    print()
    print(f"{'Varyant':32} {'Toplam':>9} {'TUPRS':>9} {'TUPRS haric':>12} "
          f"{'Medyan hisse':>13} {'En iyi hisse':>16} {'Arti/N':>8}")
    print("-" * 110)
    for etiket, s in sonuc_b:
        hg = s["hisse_getiri"]
        tuprs = hg.get("TUPRS")
        kalan = [v for k, v in hg.items() if k != "TUPRS"]
        haric = sum(kalan) / len(kalan) if kalan else 0.0
        tic, r = s["en_iyi_hisse"]
        print(f"{etiket:32} {pct(s['toplam_getiri']):>9} "
              f"{(pct(tuprs) if tuprs is not None else '-'):>9} {pct(haric):>12} "
              f"{pct(s['medyan_hisse']):>13} {tic + ' ' + pct(r):>16} "
              f"{str(s['arti_hisse']) + '/' + str(s['hisse_sayisi']):>8}")

    # ---- kuyruk bagimliligi ----
    print()
    print("=" * 110)
    print("KUYRUK BAGIMLILIGI (giris B) - kar kac isleme dayaniyor?")
    print("=" * 110)
    print("'En iyi 3/5 haric' sutunu: en karli islemler cikarilinca GERIYE KALAN")
    print("islemlerin toplami. Negatifse strateji tamamen birkac islemin esiri.")
    print()
    print(f"{'Varyant':32} {'Islem get.top.':>15} {'En iyi 1':>10} {'En iyi 3':>10} "
          f"{'En iyi 3 haric':>15} {'En iyi 5 haric':>15}")
    print("-" * 110)
    for etiket, s in sonuc_b:
        rets = sorted((t["net_getiri"] for t in s["islemler"]), reverse=True)
        toplam = sum(rets)
        print(f"{etiket:32} {pct(toplam):>15} {pct(rets[0]):>10} "
              f"{pct(sum(rets[:3])):>10} {pct(toplam - sum(rets[:3])):>15} "
              f"{pct(toplam - sum(rets[:5])):>15}")

    # ---- cikis sebebi dagilimi ----
    print()
    print("=" * 110)
    print("CIKIS SEBEBI DAGILIMI (giris B)")
    print("=" * 110)
    for etiket, s in sonuc_b:
        sayac = {}
        getiri = {}
        for t in s["islemler"]:
            sayac[t["sebep"]] = sayac.get(t["sebep"], 0) + 1
            getiri.setdefault(t["sebep"], []).append(t["net_getiri"])
        parcalar = [f"{k}: {v} ({pct(sum(getiri[k]) / v)})"
                    for k, v in sorted(sayac.items(), key=lambda kv: -kv[1])]
        print(f"{etiket:32} " + "  |  ".join(parcalar))

    print()
    print(f"UYARI: {len(EXIT_VARIANTS)} cikis varyanti denendi. Bunlarin en iyisini")
    print("secmek, gorunen performansi ISTATISTIKSEL OLARAK SISIRIR. Tablodaki")
    print("en yuksek getiri, gercek beklenen getirinin YUKARI SAPMIS tahminidir.")
    print("Yatirim tavsiyesi degildir.")


# ----------------------------------------------------------------- calistir --
def pct(x):
    return f"{x * 100:+.2f}%"


def main():
    detay = "--detay" in sys.argv
    dogrula = "--dogrula" in sys.argv

    if "--cikis" in sys.argv:
        cikis_karsilastirma()
        return

    now = datetime.now(timezone.utc)
    window_floor = now - timedelta(days=LOOKBACK_DAYS)

    print("=" * 78)
    print("SUPERTREND ALARM BACKTEST - BIST 30")
    print("=" * 78)
    print(f"Talep edilen donem   : son {LOOKBACK_DAYS} gun "
          f"({window_floor.astimezone(ISTANBUL_TZ):%Y-%m-%d} sonrasi)")
    print(f"Komisyon             : gidis-donus %{COMMISSION_ONE_WAY * 200:.1f}")
    print(f"Temel zaman dilimi   : {BASE_TF} (60 dakika)")
    print(f"Giris zaman dilimleri: {', '.join(ENTRY_TFS)}")
    print()

    results = {"A": [], "B": [], "AL_TUT": []}
    equity_sets = {"A": [], "B": [], "AL_TUT": []}

    def ilerleme(n, toplam, ticker, series, start_idx, end_idx):
        base = series[BASE_TF]
        times = base.candles
        for strat in ("A", "B"):
            trades, eq = simulate(series, strat, start_idx, end_idx)
            results[strat].append((ticker, trades))
            equity_sets[strat].append(([eq[i] for i in range(start_idx, end_idx + 1)],
                                       [times[i][0] for i in range(start_idx, end_idx + 1)]))

        bh_trades, bh_eq = buy_and_hold(series, start_idx, end_idx)
        results["AL_TUT"].append((ticker, bh_trades))
        equity_sets["AL_TUT"].append(([bh_eq[i] for i in range(start_idx, end_idx + 1)],
                                      [times[i][0] for i in range(start_idx, end_idx + 1)]))

        na = len(results["A"][-1][1])
        nb = len(results["B"][-1][1])
        print(f"[{n:2}/{toplam}] {ticker}: A={na} islem, B={nb} islem, "
              f"al-tut {pct(bh_trades[0]['net_getiri'])}")

    _evren, grid, pencere_bilgi, basarisiz, dogrulanan = load_universe(
        dogrula=dogrula, ilerleme=ilerleme)

    if not pencere_bilgi:
        print("\nHicbir hisse icin veri alinamadi, test yapilamadi.")
        return

    print()
    print("=" * 78)
    print("TEST PENCERESI")
    print("=" * 78)
    ilk = min(p[1] for p in pencere_bilgi)
    son = max(p[2] for p in pencere_bilgi)
    bar_ort = sum(p[3] for p in pencere_bilgi) / len(pencere_bilgi)
    print(f"Baslangic (ilk islem mumu) : {ilk.astimezone(ISTANBUL_TZ):%Y-%m-%d %H:%M}")
    print(f"Bitis                      : {son.astimezone(ISTANBUL_TZ):%Y-%m-%d %H:%M}")
    print(f"Hisse sayisi               : {len(pencere_bilgi)}")
    print(f"Hisse basina ortalama mum  : {bar_ort:.0f} adet 1 saatlik mum")
    if dogrula:
        print(f"Hacim bayragi dogrulamasi  : {dogrulanan} nokta, kutuphane ile birebir")
    if basarisiz:
        print(f"Atlanan hisseler           : {', '.join(t for t, _ in basarisiz)}")

    isimler = {"A": "STRATEJI A (sade 1s Supertrend)",
               "B": "STRATEJI B (coklu zaman dilimi + RSI + hacim)",
               "AL_TUT": "AL VE TUT (benchmark)"}

    ozet = {}
    for strat in ("A", "B", "AL_TUT"):
        all_trades = [t for _tic, ts in results[strat] for t in ts]
        st = trade_stats(all_trades)
        curve = portfolio_curve(equity_sets[strat], grid)
        st["toplam_getiri"] = curve[-1] - 1.0 if curve else 0.0
        st["max_dusus"] = max_drawdown(curve) if curve else 0.0
        st["egri"] = curve
        st["hisse_basi"] = {tic: ts for tic, ts in results[strat]}
        toplam_bar = sum(p[3] for p in pencere_bilgi)
        st["kalma_orani"] = (sum(t["bar"] for t in all_trades) / toplam_bar) if toplam_bar else 0.0
        ozet[strat] = st

    print()
    print("=" * 78)
    print("SONUCLAR (esit agirlikli 30 hisselik portfoy, komisyon dusulmus)")
    print("=" * 78)
    satirlar = [
        ("Islem sayisi", lambda s: f"{s['islem']}"),
        ("Kazanan / kaybeden", lambda s: f"{s.get('kazanan', 0)} / {s.get('kaybeden', 0)}"),
        ("Kazanma orani", lambda s: f"{s.get('kazanma_orani', 0) * 100:.1f}%"),
        ("Ortalama kazanc", lambda s: pct(s.get('ort_kazanc', 0))),
        ("Ortalama kayip", lambda s: pct(s.get('ort_kayip', 0))),
        ("Ortalama islem", lambda s: pct(s.get('ort_islem', 0))),
        ("En iyi islem", lambda s: pct(s.get('en_iyi', 0))),
        ("En kotu islem", lambda s: pct(s.get('en_kotu', 0))),
        ("Kar faktoru", lambda s: f"{s.get('kar_faktoru', 0):.2f}"),
        ("Ort. tutma (1s mum)", lambda s: f"{s.get('ort_bar', 0):.1f}"),
        ("Piyasada kalma orani", lambda s: f"{s['kalma_orani'] * 100:.1f}%"),
        ("TOPLAM GETIRI", lambda s: pct(s['toplam_getiri'])),
        ("MAKS. DUSUS", lambda s: pct(s['max_dusus'])),
    ]
    basliklar = ["A (sade)", "B (filtreli)", "AL VE TUT"]
    print(f"{'':24} {basliklar[0]:>16} {basliklar[1]:>16} {basliklar[2]:>16}")
    print("-" * 78)
    for etiket, fn in satirlar:
        print(f"{etiket:24} {fn(ozet['A']):>16} {fn(ozet['B']):>16} {fn(ozet['AL_TUT']):>16}")

    print()
    print("=" * 78)
    print("FILTRE KATKISI (ayni cikis kurali altinda)")
    print("=" * 78)
    a, b = ozet["A"], ozet["B"]
    print(f"B, A'nin {a['islem']} isleminden {b['islem']} tanesine denk gelen sinyal uretti "
          f"(filtre {100 * (1 - b['islem'] / a['islem']) if a['islem'] else 0:.0f}% eledi).")
    print(f"Islem basina fark (B - A): {pct(b.get('ort_islem', 0) - a.get('ort_islem', 0))}")
    print(f"Toplam getiri farki (B - A): {pct(b['toplam_getiri'] - a['toplam_getiri'])}")
    print(f"B - AL VE TUT: {pct(b['toplam_getiri'] - ozet['AL_TUT']['toplam_getiri'])}")
    print(f"A - AL VE TUT: {pct(a['toplam_getiri'] - ozet['AL_TUT']['toplam_getiri'])}")

    print()
    print("HISSE BAZINDA (net getiri katkisi, komisyon dusulmus)")
    print(f"{'Hisse':8} {'A islem':>8} {'A getiri':>10} {'B islem':>8} {'B getiri':>10} {'Al-tut':>10}")
    print("-" * 60)
    for p in pencere_bilgi:
        tic = p[0]

        def comp(strat, tic=tic):
            ts = ozet[strat]["hisse_basi"].get(tic, [])
            v = 1.0
            for t in ts:
                v *= (1 + t["net_getiri"])
            return len(ts), v - 1.0
        na, ra = comp("A")
        nb, rb = comp("B")
        _, rh = comp("AL_TUT")
        print(f"{tic:8} {na:>8} {pct(ra):>10} {nb:>8} {pct(rb):>10} {pct(rh):>10}")

    print()
    print("=" * 78)
    print("DAYANIKLILIK / GURULTU KONTROLU")
    print("=" * 78)
    bh_ret = {tic: tl[0]["net_getiri"] for tic, tl in results["AL_TUT"]}
    for strat in ("A", "B"):
        ts_all = [t for _tic, tl in results[strat] for t in tl]
        rets = [t["net_getiri"] for t in ts_all]
        mean, tval = t_stat(rets)
        per_tic = {}
        for tic, tl in results[strat]:
            v = 1.0
            for t in tl:
                v *= (1 + t["net_getiri"])
            per_tic[tic] = v - 1.0
        (en_iyi_tic, en_iyi_ret), _ = concentration(per_tic)
        kalan = [v for k, v in per_tic.items() if k != en_iyi_tic]
        medyan = sorted(per_tic.values())[len(per_tic) // 2]
        yenen = sum(1 for k, v in per_tic.items() if v > bh_ret.get(k, 0.0))
        sirali = sorted(rets, reverse=True)
        top3 = sum(sirali[:3])
        print(f"\n{isimler[strat]}")
        print(f"  Islem sayisi                         : {len(rets)} "
              f"({len(rets) / len(pencere_bilgi):.1f} islem/hisse)")
        print(f"  Islem basina ort. getiri (t-istat.)   : {pct(mean)}  (t = {tval:.2f})")
        print(f"  Hisse bazinda medyan getiri          : {pct(medyan)}")
        print(f"  Al-tut'u geciren hisse sayisi        : {yenen} / {len(per_tic)}")
        print(f"  En cok katki yapan hisse             : {en_iyi_tic} ({pct(en_iyi_ret)})")
        print(f"  O hisse cikarilinca ort. hisse getirisi: "
              f"{pct(sum(kalan) / len(kalan))} (once {pct(sum(per_tic.values()) / len(per_tic))})")
        print(f"  En iyi 3 islemin toplam getirideki payi: {pct(top3)} / "
              f"{pct(sum(rets))} toplam islem getirisi")

    if detay:
        for strat in ("A", "B"):
            print()
            print("=" * 78)
            print(f"ISLEM LISTESI - {isimler[strat]}")
            print("=" * 78)
            for tic, ts in results[strat]:
                for t in ts:
                    flag = " (veri sonunda acik)" if t["acik_kapatildi"] else ""
                    print(f"{tic:7} {t['giris_dt'].astimezone(ISTANBUL_TZ):%Y-%m-%d %H:%M} -> "
                          f"{t['cikis_dt'].astimezone(ISTANBUL_TZ):%Y-%m-%d %H:%M} "
                          f"{t['giris']:8.2f} -> {t['cikis']:8.2f}  "
                          f"brut {pct(t['brut_getiri'])}  net {pct(t['net_getiri'])}"
                          f"  {t['bar']:3} mum{flag}")

    print()
    print("Yatirim tavsiyesi degildir. Gecmis performans gelecegi garanti etmez.")


if __name__ == "__main__":
    main()
