#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
/tara KOMUTUNUN GERIYE DONUK TESTI - "ilk 10'u al, 1 ay tut"
=============================================================
Soru: /tara'nin GECMIS bir gunde onerecegi ilk 10 hisseyi esit agirlikla alip
1 ay tutsaydim ne olurdu? 1.000.000 TL bugun ne olurdu?

Bu dosya backtest.py'yi BOZMAZ, ondan import eder: Series sinifi, index_asof(),
nokta-zamanli gosterge hesabi, hacim bayragi, mum onbellegi hepsi oradan gelir.

YONTEM
------
1) Karar ani T = bir borsa gunu KAPANDIKTAN sonra ayni aksam (Istanbul 20:00,
   bkz. KARAR_SAAT - 18:00 degil, cunku BIST'in son mumlari 18:00'de kapanir
   ve damga+periyot kuralina gore ancak 18:30/19:00'da "kapanmis" sayilir).
   O anda
   supertrend_sorgu_bot.hisse_puani() ile BIREBIR ayni puan hesaplanir:
   her zaman dilimi icin Supertrend AL yonu (1) + RSI teyidi (1) +
   yuksek hacim (1). En yuksek puanli 10 hisse alinir (TARA_TOP_N).
   Siralama anahtari de birebir ayni: (puan, al, hacim) buyukten kucuge,
   beraberlikte alfabetik (BIST30_TICKERS zaten alfabetik, sort kararli).
2) Alim ERTESI borsa gununun ACILISINDAN yapilir (karar aninda bu fiyat
   bilinemez -> gelecege bakma yok).
3) 1 ay = 21 BORSA GUNU. Cikis, giristen 21 borsa gunu sonraki gunun
   ACILISINDAN. Giris ve cikis ayni fiyat tipini (acilis) kullanir ki
   acilis/kapanis asimetrisi sonuca karismasin.
4) Her hisseye 100.000 TL (1.000.000 / 10), yeniden dengeleme yok.
   Komisyon gidis-donus %0.2 (backtest.COMMISSION_ONE_WAY).

LOOK-AHEAD (GELECEGE BAKMA) ONLEMLERI
--------------------------------------
* Her zaman dilimi icin puan, backtest.Series.index_asof(T) ile "kapanis ani
  <= T olan SON mum"dan okunur. Henuz kapanmamis mum asla gorulmez.
* Gostergeler (Supertrend, RSI, hacim orani) ozyineli ama NEDENSEL: j'inci
  deger yalnizca 0..j mumlarina bagli. backtest.py bunu --dogrula ile
  kutuphane fonksiyonlarina karsi zaten test ediyor; burada da kosulur.
* Mum KAPANIS ANLARI borsa takvimine gore duzeltildi (asagidaki TFSeries):
  Yahoo'nun zaman damgasi mumun BASLANGICIDIR. Gun ici mumlarda
  kapanis = damga + periyot (backtest.py'deki gibi), ama
    - GUNLUK mumun damgasi 09:30 (acilis) -> kapanis = ayni gun 18:00,
    - HAFTALIK mumun damgasi Pazartesi 00:00 -> kapanis = o haftanin Cuma 18:00.
  Bu, canli bottaki candle_is_closed() ile BIREBIR ayni kuraldir. Damga+periyot
  kullansaydik backtest, canli botun o anda GORDUGU gunluk mumu goremezdi.
* Isinma: backtest.Series.first_valid (gosterge isinmasi + 100 mum tohum
  arinmasi). Bir zaman dilimi karar aninda henuz isinmamissa o hisse o
  pencerede EVRENE ALINMAZ (ne /tara'ya ne de karsilastirma olcutlerine).
* Fiyatlar ayri bir gunluk seriden okunur; gosterge isinmasiyla ilgisi yok.

KARSILASTIRMA OLCUTLERI (ayni pencere, AYNI uygun evren)
---------------------------------------------------------
(a) Evrenin tamamina esit dagitim (BIST30 esit agirlikli).
(b) Rastgele 10 hisse: her pencerede binlerce rastgele 10'lu cekilir.
    /tara'nin sonucu bu dagilimin kacinci yuzdelik diliminde? "Sans mi
    beceri mi" sorusunun tek durust cevabi budur.
    Not: rastgele-10'un BEKLENEN degeri, tanim geregi (a) ile ayni. Dagilimin
    genisligi bize "10 hisse secmenin gurultusu" ne kadar buyuk onu soyler.

VERI SINIRI (olculen, varsayilan degil)
----------------------------------------
Yahoo'da 5m VE 15m serileri en fazla ~60 GUN geriye gider (ikisi de!).
60m ~2 yildan fazla, 1d/1wk daha da fazla. Dolayisiyla:
  * "tam"  (7 dilim, 5dk dahil)          -> sadece ~60 gun -> COK AZ pencere
  * "6tf"  (5dk yok, 15dk var)           -> yine ~60 gun (15dk da sinirli!)
  * "5tf"  (5dk+15dk yok: 1s/2s/4s/1g/1hf) -> ~2.5 yil -> istatistiksel agirlik
"6tf" varyantinin gorevde beklenen faydayi SAGLAMADIGI olculdu; "5tf"
eklenerek uzun ornek elde edildi. "5tf" tam olarak /tara DEGILDIR.

KULLANIM
--------
    BACKTEST_CACHE_DIR=/tmp/bt_cache python3 backtest_tara.py
    ... --varyant 5tf        # tek varyant (tam | 6tf | 5tf | hepsi)
    ... --adim 5 --tut 21    # kaydirma adimi / tutma suresi (borsa gunu)
    ... --sim 20000          # rastgele-10 Monte Carlo tekrar sayisi
    ... --dogrula            # hacim bayragi ic tutarlilik kontrolu
    ... --pencereler         # her pencereyi tek tek listele

Yatirim tavsiyesi degildir.
"""

import bisect
import math
import random
import sys
from datetime import datetime, time as dt_time, timedelta, timezone

import backtest
from supertrend_alarm import (
    BIST30_TICKERS,
    ISTANBUL_TZ,
    resample_ohlc,
    rsi_confirms,
)

# ------------------------------------------------------------------ ayarlar --
TARA_TIMEFRAMES = ("5dk", "15dk", "1s", "2s", "4s", "1g", "1hf")   # bot ile ayni
TARA_TOP_N = 10                                                     # bot ile ayni
SERMAYE = 1_000_000.0
HOLD_DAYS = 21          # 1 ay = 21 borsa gunu
STEP_DAYS = 5           # kayan pencere adimi (haftada bir)
MC_SIMS = 20_000        # rastgele-10 Monte Carlo tekrari
MC_SEED = 20260912
BORSA_KAPANIS_SAAT = 18  # Istanbul; candle_is_closed() ile ayni
# Karar ani: seans bittikten SONRA (kullanici /tara'yi aksam calistirir).
# 18:00 SECILMEDI cunku BIST'in son 60dk mumu 17:30'da baslar ve 18:00'de
# (yarim mum) kapanir; ayrica 18:00'de kapanis muzayedesi mumu gelir. Damga +
# periyot kuralina gore bunlarin "kapanis ani" 18:30/19:00'dur, yani tam
# 18:00'de bakarsak gunun son 1-2 saatlik mumunu KACIRIRIZ ve canli botun
# gordugu tabloyla uyusmaz (olculdu: 1s diliminde %74 uyum). 20:00'de butun
# gun kapanmis, ertesi gunden hicbir veri yok -> gelecege bakma hala imkansiz.
KARAR_SAAT = 20

# Varyantlar: (anahtar, ekran adi, zaman dilimleri)
VARYANTLAR = [
    ("tam", "GERCEK /tara (7 dilim, 21 puan)", ("5dk", "15dk", "1s", "2s", "4s", "1g", "1hf")),
    ("6tf", "5dk'siz (6 dilim, 18 puan)", ("15dk", "1s", "2s", "4s", "1g", "1hf")),
    ("5tf", "gun-ici kisitsiz (5 dilim, 15 puan)", ("1s", "2s", "4s", "1g", "1hf")),
]

# Veri cekme plani: (Yahoo interval, Yahoo range, 60dk'dan yeniden orneklerken
# grup buyuklugu). backtest.FETCH_PLAN'in uzun-gecmis surumu; canli bottaki
# TIMEFRAME_CONFIG kisa araliklar kullanir (bkz. CLAUDE.md "Bilinen sinirlar").
TARA_FETCH_PLAN = {
    "5dk":  ("5m",  "60d",  None),   # Yahoo siniri: ~60 gun
    "15dk": ("15m", "60d",  None),   # Yahoo siniri: ~60 gun (2y BOS donuyor)
    "1s":   ("60m", "730d", None),
    "2s":   ("60m", "730d", 2),
    "4s":   ("60m", "730d", 4),
    "1g":   ("1d",  "5y",   None),
    "1hf":  ("1wk", "10y",  None),
}
FIYAT_INTERVAL, FIYAT_RANGE = "1d", "5y"

# backtest.Series, adim suresini backtest.FETCH_PLAN + backtest.INTERVAL_SECONDS
# uzerinden okuyor. Modul sozluklerine YALNIZCA YENI anahtarlar eklenir; mevcut
# anahtarlara dokunulmaz, dolayisiyla backtest.py'nin kendi davranisi degismez.
backtest.INTERVAL_SECONDS.setdefault("1d", 86400)
backtest.INTERVAL_SECONDS.setdefault("1wk", 604800)
for _lbl, _plan in TARA_FETCH_PLAN.items():
    backtest.FETCH_PLAN.setdefault(_lbl, _plan)


# ------------------------------------------------- borsa takvimli kapanis --
def _gun_kapanisi(local_date):
    """Bir borsa gununun kapanis ani (Istanbul 18:00) -> UTC."""
    return datetime.combine(local_date, dt_time(BORSA_KAPANIS_SAAT, 0),
                            tzinfo=ISTANBUL_TZ).astimezone(timezone.utc)


def _karar_ani(local_date):
    """O gunun /tara karar ani (seans kapandiktan sonra, ayni aksam)."""
    return datetime.combine(local_date, dt_time(KARAR_SAAT, 0),
                            tzinfo=ISTANBUL_TZ).astimezone(timezone.utc)


def _exchange_close_time(label, ts_utc):
    local = ts_utc.astimezone(ISTANBUL_TZ)
    if label == "1g":
        return _gun_kapanisi(local.date())
    # 1hf: damga Pazartesi 00:00; hafta Cuma 18:00'de kapanir
    cuma = local.date() + timedelta(days=(4 - local.weekday()) % 7)
    return _gun_kapanisi(cuma)


class TFSeries(backtest.Series):
    """backtest.Series + gunluk/haftalik mumlar icin borsa takvimine gore
    duzeltilmis kapanis anlari. Gun ici dilimlerde hicbir sey degismez."""

    def __init__(self, label, candles):
        super().__init__(label, candles)
        if label in ("1g", "1hf"):
            self.close_times = [_exchange_close_time(label, c[0]) for c in candles]
            assert all(self.close_times[i] < self.close_times[i + 1]
                       for i in range(self.n - 1)), f"{label}: kapanis anlari artan degil"


def _temizle_haftalik(candles):
    """Yahoo 1wk serisinin SONUNA, hafta mumu gibi gorunen ama aslinda son
    isleme ait anlik bir goruntu ekliyor (damgasi Pazartesi 00:00 degil,
    orn. Cuma 18:09). Gercek hafta mumlarinin tamami Pazartesi 00:00'dir;
    digerleri atilir. (Canli bot bunu ATMIYOR - ayri bir bulgu, rapora yazildi.)"""
    out = []
    for c in candles:
        local = c[0].astimezone(ISTANBUL_TZ)
        if local.weekday() == 0 and local.hour == 0 and local.minute == 0:
            out.append(c)
    return out


# ------------------------------------------------------------- veri yukleme --
def _mumlar(ticker, label):
    interval, rng, group = TARA_FETCH_PLAN[label]
    candles = backtest.fetch_raw(ticker, interval, rng)
    if label == "1hf":
        candles = _temizle_haftalik(candles)
    if group:
        candles = resample_ohlc(candles, group)
    return candles


def hisse_yukle(ticker, labels, sessiz=False):
    """Doner: (seriler, gunluk_fiyat). seriler[label] = TFSeries ya da None."""
    seriler = {}
    for label in labels:
        try:
            candles = _mumlar(ticker, label)
            if len(candles) < 60:
                seriler[label] = None
                continue
            seriler[label] = TFSeries(label, candles)
        except Exception as exc:
            if not sessiz:
                print(f"    {ticker} {label}: veri hatasi ({exc})")
            seriler[label] = None

    gunluk = backtest.fetch_raw(ticker, FIYAT_INTERVAL, FIYAT_RANGE)
    fiyat = {}
    for c in gunluk:
        d = c[0].astimezone(ISTANBUL_TZ).date()
        fiyat[d] = (c[1], c[4])     # (acilis, kapanis)
    return seriler, fiyat


# -------------------------------------------------------------- puanlama --
def puanla(seriler, labels, T):
    """supertrend_sorgu_bot.hisse_puani()'nin nokta-zamanli esdegeri.
    Bir zaman dilimi T aninda henuz isinmamissa None doner (hisse elenir)."""
    al = rsi = hacim = 0
    for label in labels:
        s = seriler.get(label)
        if s is None:
            return None
        j = s.index_asof(T)
        if j is None or j < s.first_valid:
            return None
        if s.dirs[j] == 1:
            al += 1
        # DIKKAT: canli bot (get_timeframe_status) RSI teyidini O ANKI
        # Supertrend YONUNE gore verir - SAT yonundeyken 30<RSI<50 de puan
        # kazandirir. Kulaga ters geliyor ama /tara aynen boyle davraniyor;
        # burada da aynen taklit edilir, yoksa test /tara'yi olcmus olmaz.
        if rsi_confirms(s.rsi[j], s.dirs[j]):
            rsi += 1
        if s.hivol[j]:
            hacim += 1
    return {"al": al, "rsi": rsi, "hacim": hacim, "puan": al + rsi + hacim}


# ---------------------------------------------------------------- pencere --
NET = (1 - backtest.COMMISSION_ONE_WAY) ** 2


def pencere_calistir(veri, labels, takvim, k, hold):
    """k = karar gununun takvimdeki indeksi. Doner: pencere sozlugu ya da None."""
    if k + 1 + hold >= len(takvim):
        return None
    karar_gun = takvim[k]
    giris_gun = takvim[k + 1]
    cikis_gun = takvim[k + 1 + hold]
    T = _karar_ani(karar_gun)

    evren = []      # (ticker, puan_sozlugu, net_getiri)
    for ticker in sorted(veri):
        seriler, fiyat = veri[ticker]
        if giris_gun not in fiyat or cikis_gun not in fiyat:
            continue
        p = puanla(seriler, labels, T)
        if p is None:
            continue
        giris = fiyat[giris_gun][0]
        cikis = fiyat[cikis_gun][0]
        if not giris or not cikis or giris <= 0:
            continue
        evren.append((ticker, p, (cikis / giris) * NET - 1.0))

    if len(evren) < TARA_TOP_N + 5:     # anlamli bir secim icin cok dar evren
        return None

    sirali = sorted(evren, key=lambda r: (r[1]["puan"], r[1]["al"], r[1]["hacim"]),
                    reverse=True)
    secilen = sirali[:TARA_TOP_N]
    tara_ret = sum(r[2] for r in secilen) / len(secilen)
    evren_ret = sum(r[2] for r in evren) / len(evren)

    return {
        "karar": karar_gun, "giris": giris_gun, "cikis": cikis_gun,
        "evren_n": len(evren),
        "getiriler": [r[2] for r in evren],
        "secilen": [(r[0], r[1]["puan"], r[2]) for r in secilen],
        "tara": tara_ret,
        "evren": evren_ret,
        "esik_puan": secilen[-1][1]["puan"],
        "en_yuksek_puan": secilen[0][1]["puan"],
    }


# -------------------------------------------------------- istatistik yardim --
def ortalama(xs):
    return sum(xs) / len(xs) if xs else 0.0


def medyan(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def stdsapma(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = ortalama(xs)
    return (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5


def yuzdelik(dagilim_sirali, deger):
    """deger, sirali dagilimin kacinci yuzdelik diliminde (0-100)."""
    lo = bisect.bisect_left(dagilim_sirali, deger)
    hi = bisect.bisect_right(dagilim_sirali, deger)
    return 100.0 * ((lo + hi) / 2) / len(dagilim_sirali)


def binom_iki_yonlu(k, n):
    """H0: p=0.5 altinda, en az k basari kadar "asiri" olma olasiligi."""
    if n == 0:
        return 1.0
    pmf = [math.comb(n, i) / 2 ** n for i in range(n + 1)]
    esik = pmf[k] * (1 + 1e-9)
    return min(1.0, sum(p for p in pmf if p <= esik))


def tl(x):
    return f"{x:,.0f} TL".replace(",", ".")


def pct(x):
    return f"{x * 100:+.2f}%"


# ------------------------------------------------------------- Monte Carlo --
def rastgele_analiz(pencereler, sims, seed):
    """Her pencerede rastgele 10 hisse seciliyor. Doner:
       - pencere basina /tara'nin yuzdelik dilimi
       - pencereler ORTALAMASI uzerinden kurulan 'rastgele strateji' dagilimi"""
    rng = random.Random(seed)
    n_w = len(pencereler)
    per_window_pct = []
    # her pencere icin sims adet rastgele-10 getirisi (ayni cekimler hem
    # yuzdelik hem strateji dagilimi icin kullanilir)
    cekimler = []
    for w in pencereler:
        rets = w["getiriler"]
        idx = list(range(len(rets)))
        col = []
        for _ in range(sims):
            sec = rng.sample(idx, TARA_TOP_N)
            col.append(sum(rets[i] for i in sec) / TARA_TOP_N)
        cekimler.append(col)
        per_window_pct.append(yuzdelik(sorted(col), w["tara"]))

    strateji = [ortalama([cekimler[j][s] for j in range(n_w)]) for s in range(sims)]
    strateji.sort()
    return per_window_pct, strateji


# ------------------------------------------------------------------ rapor --
def varyant_raporu(ad, labels, veri, takvim, adim, hold, sims, detay):
    print()
    print("=" * 100)
    print(f"VARYANT: {ad}")
    print("=" * 100)
    print(f"Zaman dilimleri : {', '.join(labels)}  (maks. {len(labels) * 3} puan)")

    pencereler = []
    for k in range(0, len(takvim), adim):
        w = pencere_calistir(veri, labels, takvim, k, hold)
        if w:
            pencereler.append(w)

    if not pencereler:
        print("Hicbir gecerli pencere kurulamadi (veri yetersiz).")
        return None

    ilk, son = pencereler[0], pencereler[-1]
    print(f"Karar tarihleri : {ilk['karar']} .. {son['karar']}  "
          f"(adim {adim} borsa gunu, tutma {hold} borsa gunu)")
    print(f"Pencere sayisi  : {len(pencereler)}  "
          f"(ortusen! bagimsiz degil - asagida ortusmeyen alt orneklem de var)")
    ort_evren = ortalama([w["evren_n"] for w in pencereler])
    print(f"Uygun evren     : ortalama {ort_evren:.1f} hisse "
          f"(min {min(w['evren_n'] for w in pencereler)}, "
          f"maks {max(w['evren_n'] for w in pencereler)})")
    print(f"Secim esigi     : ortalama tepe puan "
          f"{ortalama([w['en_yuksek_puan'] for w in pencereler]):.1f}, "
          f"10. sira {ortalama([w['esik_puan'] for w in pencereler]):.1f} "
          f"/ {len(labels) * 3}")

    tara = [w["tara"] for w in pencereler]
    evren = [w["evren"] for w in pencereler]
    fark = [t - e for t, e in zip(tara, evren)]

    # --- 1) getiri tablosu ---
    print()
    print("-" * 100)
    print("1 AYLIK (21 borsa gunu) GETIRILER - 1.000.000 TL, esit agirlikli 10 hisse")
    print("-" * 100)
    print(f"{'':22} {'/tara ilk 10':>18} {'Tum evren (esit)':>18} {'Fark':>12}")
    satirlar = [
        ("Ortalama pencere", ortalama(tara), ortalama(evren)),
        ("Medyan pencere", medyan(tara), medyan(evren)),
        ("Std. sapma", stdsapma(tara), stdsapma(evren)),
        ("En iyi pencere", max(tara), max(evren)),
        ("En kotu pencere", min(tara), min(evren)),
    ]
    for etiket, a, b in satirlar:
        print(f"{etiket:22} {pct(a):>18} {pct(b):>18} {pct(a - b):>12}")
    arti = sum(1 for t in tara if t > 0)
    print(f"{'Artida biten pencere':22} {f'{arti}/{len(tara)}':>18} "
          f"{f'{sum(1 for e in evren if e > 0)}/{len(evren)}':>18}")

    print()
    print(f"1.000.000 TL -> ORTALAMA pencere sonu : {tl(SERMAYE * (1 + ortalama(tara)))} "
          f"(evren: {tl(SERMAYE * (1 + ortalama(evren)))})")
    print(f"1.000.000 TL -> MEDYAN  pencere sonu : {tl(SERMAYE * (1 + medyan(tara)))} "
          f"(evren: {tl(SERMAYE * (1 + medyan(evren)))})")
    print(f"1.000.000 TL -> EN IYI  pencere ({max(pencereler, key=lambda w: w['tara'])['karar']}): "
          f"{tl(SERMAYE * (1 + max(tara)))}")
    print(f"1.000.000 TL -> EN KOTU pencere ({min(pencereler, key=lambda w: w['tara'])['karar']}): "
          f"{tl(SERMAYE * (1 + min(tara)))}")
    print("NOT: pencereler ortustugu icin BIRLESTIRILMEZ. Her satir 'o gun 1.000.000")
    print("     TL koysaydim 1 ay sonra' sorusunun cevabidir, ust uste bindirilmis")
    print("     bir sermaye buyumesi DEGILDIR.")

    # --- 2) ortusmeyen alt orneklem ---
    bagimsiz = pencereler[::max(1, math.ceil(hold / adim))]
    if len(bagimsiz) >= 2:
        bt = [w["tara"] for w in bagimsiz]
        be = [w["evren"] for w in bagimsiz]
        print()
        print(f"ORTUSMEYEN alt orneklem ({len(bagimsiz)} pencere, birbirine deymiyor):")
        print(f"  /tara ortalama {pct(ortalama(bt))} | evren {pct(ortalama(be))} | "
              f"fark {pct(ortalama(bt) - ortalama(be))}")
        k_iyi = sum(1 for a, b in zip(bt, be) if a > b)
        print(f"  /tara evreni {k_iyi}/{len(bt)} penceresinde yendi  "
              f"(isaret testi p = {binom_iki_yonlu(max(k_iyi, len(bt) - k_iyi), len(bt)):.3f})")

    # --- 3) rastgele 10 karsilastirmasi ---
    print()
    print("-" * 100)
    print(f"RASTGELE 10 HISSE ILE KARSILASTIRMA ({sims:,} tekrar/pencere)".replace(",", "."))
    print("-" * 100)
    per_w_pct, strateji = rastgele_analiz(pencereler, sims, MC_SEED)
    p_tara = yuzdelik(strateji, ortalama(tara))
    print(f"Rastgele-10 'strateji' dagilimi (pencere ortalamasi uzerinden):")
    print(f"  ortalama {pct(ortalama(strateji))}   std {pct(stdsapma(strateji))}")
    for q in (1, 5, 25, 50, 75, 95, 99):
        print(f"  %{q:<3} dilim: {pct(strateji[int(q / 100 * (len(strateji) - 1))])}")
    print()
    print(f">>> /tara ortalamasi {pct(ortalama(tara))} -> rastgele-10 dagiliminda "
          f"%{p_tara:.1f}'lik dilim")
    print(f"    (rastgele bir secimin /tara'yi gecme olasiligi: %{100 - p_tara:.1f})")
    print()
    print(f"Pencere bazinda yuzdelik dilim: ortalama %{ortalama(per_w_pct):.1f}, "
          f"medyan %{medyan(per_w_pct):.1f}")
    print("  (saf gurultu olsaydi ikisi de ~%50 cikardi; dagilim duzgun yayilirdi)")
    kova = [0] * 10
    for p in per_w_pct:
        kova[min(9, int(p // 10))] += 1
    print("  dilim histogrami (0-10 / 10-20 / ... / 90-100):")
    print("   " + " ".join(f"{c:3}" for c in kova))

    # --- 4) fark testi ---
    print()
    m, sd = ortalama(fark), stdsapma(fark)
    t = m / (sd / len(fark) ** 0.5) if sd > 0 else 0.0
    k_iyi = sum(1 for f in fark if f > 0)
    print(f"Pencere basina (tara - evren) farki: ortalama {pct(m)}, std {pct(sd)}, "
          f"t = {t:.2f}  [ORTUSEN pencereler -> t SISIRILMIS, oldugundan buyuk]")
    print(f"/tara evreni {k_iyi}/{len(fark)} penceresinde yendi")

    if detay:
        print()
        print("-" * 100)
        print("PENCERELER")
        print("-" * 100)
        print(f"{'Karar':12} {'Giris':12} {'Cikis':12} {'N':>3} {'Tepe':>5} "
              f"{'/tara':>9} {'Evren':>9} {'Fark':>9}  Ilk 3")
        for w in pencereler:
            ilk3 = ", ".join(f"{t}({p})" for t, p, _ in w["secilen"][:3])
            print(f"{str(w['karar']):12} {str(w['giris']):12} {str(w['cikis']):12} "
                  f"{w['evren_n']:>3} {w['en_yuksek_puan']:>5} "
                  f"{pct(w['tara']):>9} {pct(w['evren']):>9} "
                  f"{pct(w['tara'] - w['evren']):>9}  {ilk3}")

    return {"ad": ad, "n": len(pencereler), "tara": tara, "evren": evren,
            "pct": p_tara, "per_w_pct": per_w_pct, "pencereler": pencereler}


# ------------------------------------------------------------------- main --
def main():
    adim = STEP_DAYS
    hold = HOLD_DAYS
    sims = MC_SIMS
    secilen_varyant = "hepsi"
    detay = "--pencereler" in sys.argv
    dogrula = "--dogrula" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--adim" and i + 1 < len(sys.argv):
            adim = int(sys.argv[i + 1])
        if a == "--tut" and i + 1 < len(sys.argv):
            hold = int(sys.argv[i + 1])
        if a == "--sim" and i + 1 < len(sys.argv):
            sims = int(sys.argv[i + 1])
        if a == "--varyant" and i + 1 < len(sys.argv):
            secilen_varyant = sys.argv[i + 1]

    varyantlar = [v for v in VARYANTLAR
                  if secilen_varyant in ("hepsi", v[0])]
    if not varyantlar:
        print(f"Bilinmeyen varyant: {secilen_varyant}")
        return

    labels_gerekli = sorted({l for _k, _a, ls in varyantlar for l in ls},
                            key=lambda x: TARA_TIMEFRAMES.index(x))

    print("=" * 100)
    print("/tara ILK 10 - 1 AY TUT - GERIYE DONUK TEST")
    print("=" * 100)
    print(f"Sermaye        : {tl(SERMAYE)} (hisse basina {tl(SERMAYE / TARA_TOP_N)})")
    print(f"Komisyon       : gidis-donus %{backtest.COMMISSION_ONE_WAY * 200:.1f}")
    print(f"Tutma          : {hold} borsa gunu  |  Kayan pencere adimi: {adim} borsa gunu")
    print(f"Yurutme        : karar gunun KAPANISINDA, alim ERTESI gun ACILISTAN,")
    print(f"                 satis {hold} borsa gunu sonrasinin ACILISINDAN")
    print(f"Evren          : BIST 30 (bugunku liste - HAYATTA KALMA YANLILIGI var)")
    print()

    print("Veri yukleniyor (onbellek: %s)..." %
          (backtest.CACHE_DIR or "YOK - her sey Yahoo'dan cekilecek"))
    veri = {}
    for n, ticker in enumerate(BIST30_TICKERS, 1):
        seriler, fiyat = hisse_yukle(ticker, labels_gerekli)
        eksik = [l for l in labels_gerekli if seriler.get(l) is None]
        veri[ticker] = (seriler, fiyat)
        durum = f"eksik: {','.join(eksik)}" if eksik else "tam"
        print(f"[{n:2}/{len(BIST30_TICKERS)}] {ticker:6} gunluk {len(fiyat):4} mum  {durum}")

    if dogrula:
        print()
        print("Hacim bayragi dogrulamasi (backtest.verify_volume_flags)...")
        toplam = 0
        for ticker, (seriler, _f) in veri.items():
            s = {k: v for k, v in seriler.items()
                 if v is not None and v.n > v.first_valid + 1}
            toplam += backtest.verify_volume_flags(s)
        print(f"  {toplam} noktada kutuphane fonksiyonlariyla BIREBIR ayni.")

    # ortak borsa takvimi: en az 20 hissenin mum verdigi gunler
    sayac = {}
    for _t, (_s, fiyat) in veri.items():
        for d in fiyat:
            sayac[d] = sayac.get(d, 0) + 1
    takvim = sorted(d for d, c in sayac.items() if c >= 20)
    print()
    print(f"Ortak borsa takvimi: {len(takvim)} gun "
          f"({takvim[0]} .. {takvim[-1]})")

    sonuclar = []
    for _key, ad, labels in varyantlar:
        r = varyant_raporu(ad, labels, veri, takvim, adim, hold, sims, detay)
        if r:
            sonuclar.append(r)

    if len(sonuclar) > 1:
        print()
        print("=" * 100)
        print("OZET")
        print("=" * 100)
        print(f"{'Varyant':40} {'Pencere':>8} {'/tara ort':>11} {'Evren ort':>11} "
              f"{'Fark':>9} {'Rastgele dilim':>16}")
        print("-" * 100)
        for r in sonuclar:
            dilim = "%%%.1f" % r["pct"]
            print(f"{r['ad']:40} {r['n']:>8} {pct(ortalama(r['tara'])):>11} "
                  f"{pct(ortalama(r['evren'])):>11} "
                  f"{pct(ortalama(r['tara']) - ortalama(r['evren'])):>9} "
                  f"{dilim:>16}")

    print()
    print("Yatirim tavsiyesi degildir. Gecmis performans gelecegi garanti etmez.")


if __name__ == "__main__":
    main()
