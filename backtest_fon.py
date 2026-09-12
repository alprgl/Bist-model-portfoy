#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FON MODEL PORTFÖY BACKTEST
==========================
fon_model_portfoy.py'deki seçim kuralının geçmişte ne yapacağını ölçer.
fon_model_portfoy.py'yi HİÇ değiştirmez, import da etmez (o dosya import
edilince ağ/dosya yan etkileri olmadan da olsa sabitleri kopyalamak yerine
burada bilinçli olarak yeniden yazıldı - kural değişirse ikisi ayrışır,
bu yüzden aşağıdaki sabitler oradan elle senkron tutulmalı).

İki ayrı ölçüm yapar:

  1) GERÇEKLEŞEN: fon_portfoy.csv'deki mevcut sepete (2026-09-02) gerçekten
     1.000.000 TL konsaydı bugün ne olurdu. Tahmin değil, TEFAS'ın kendi
     fiyatlarıyla doğrulanabilir bir hesap.

  2) YENİDEN KURULMUŞ (as-of) TEST: geçmişteki her seçim tarihi için, SADECE
     o tarihe kadarki TEFAS verisiyle skorlama yeniden kurulur, kuralın o gün
     seçeceği 6 fon bulunur, 1.000.000 TL eşit dağıtılır, ~1 ay tutulur.

VALÖR VARSAYIMI
---------------
Karar tarihi D'de SADECE tarihi <= D olan veri kullanılır. Alış, D'den SONRAKİ
ilk işlem gününün fiyatından yapılır (TEFAS'ta fon alım emri genelde ertesi iş
günü fiyatıyla gerçekleşir). Satış da aynı şekilde D+30'dan sonraki ilk işlem
gününün fiyatından yapılır. Komisyon/giriş-çıkış ücreti yok sayılmıştır
(TEFAS'ta çoğu fonda yok, ama serbest fonlarda olabilir - bkz. rapor uyarıları).

KULLANIM
--------
    python3 backtest_fon.py              # tam rapor
    python3 backtest_fon.py --gerceklesen  # sadece 1. bölüm (hızlı)
"""

import calendar
import csv
import gzip
import json
import random
import statistics
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

# =============================================================================
# AYARLAR - fon_model_portfoy.py ile ELLE senkron tutulur
# =============================================================================

API_BASE = "https://www.tefas.gov.tr/api/funds"
FON_GENEL_URL = f"{API_BASE}/fonGnlBlgSiraliGetir"
FON_TUR_URL = f"{API_BASE}/fonTurGetir"

CACHE_DIR = Path("/tmp/tefas_cache")
FON_PORTFOLIO_FILE = Path(__file__).resolve().parent / "fon_portfoy.csv"

# --- fon_model_portfoy.py'den birebir kopya sabitler ---
FON_PORTFOLIO_SIZE = 6
FON_PORTFOLIO_MIN_SKOR = 80.0
FON_PORTFOLIO_MIN_AYLIK_GETIRI = 15.0
LOOKBACK_DAYS = 30
RISK_MIN_PORTFOY_BUYUKLUK = 10_000_000.0
RISK_MIN_KISI_SAYISI = 10
KATMAN_AGIRLIKLARI = {"katman_a_getiri": 0.45, "katman_b_akis": 0.20, "katman_c_risk": 0.35}
SHARPE_VOLATILITE_TABAN = 0.01
AKIS_Z_MIN_GOZLEM = 10
UZUN_VADE_DONEMLERI = [("1a", 1), ("3a", 3), ("6a", 6), ("1y", 12)]
REFERANS_PENCERE_GUN = 6

# --- backtest'e özel ---
YATIRIM_TL = 1_000_000.0
TUTMA_GUN = 30              # kaç takvim günü tutulacak
# Akis z-skoru icin fonun kendi gecmisini kac ISLEM gunu geriye kurgulayacagiz.
# Canli kod bunu birikmis CSV'den okur (elde sadece 4 gun var), burada TEFAS ham
# verisinden GUNLUK olarak yeniden kuruluyor - yani canli kosudan daha dolu.
AKIS_GECMIS_GUN = 20
DONEM_SAYISI = 10           # kaç ayrı karar tarihi (ardışık aylar, geriye doğru)
RASTGELE_TEKRAR = 2000      # rastgele-6 dağılımı için kaç çekiliş
RASTGELE_SEED = 20260912

REQUEST_TIMEOUT_SEC = 90
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
    "Origin": "https://www.tefas.gov.tr",
    "Referer": "https://www.tefas.gov.tr/TarihselVeriler.aspx",
}


def n_ay_once(tarih: date, ay_sayisi: int) -> date:
    toplam_ay = tarih.year * 12 + (tarih.month - 1) - ay_sayisi
    yil, ay = divmod(toplam_ay, 12)
    ay += 1
    son_gun = calendar.monthrange(yil, ay)[1]
    return date(yil, ay, min(tarih.day, son_gun))


# =============================================================================
# AĞ + DİSK ÖNBELLEĞİ
# =============================================================================

def tefas_post(url: str, payload: dict, retries: int = 8):
    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, headers=REQUEST_HEADERS, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                wait = min(60.0, 5.0 * attempt * attempt)
                print(f"  [uyari] 429, {wait:.0f}sn bekleniyor ({attempt}/{retries})...")
                time.sleep(wait)
            else:
                raise
        except Exception as e:                       # timeout / gecici ag hatasi
            last_err = e
            time.sleep(2.0 * attempt)
    raise RuntimeError(f"{url} alinamadi: {last_err}")


def _cache_path(bas: date, bit: date) -> Path:
    return CACHE_DIR / f"fon_{bas:%Y%m%d}_{bit:%Y%m%d}.json.gz"


def fetch_window(bas: date, bit: date):
    """[bas, bit] araligindaki TUM fon satirlarini ceker; diske gzip onbellege alir.
    TEFAS 1 aydan uzun araligi reddettigi icin arayan taraf <=30 gunluk parcalar
    vermelidir."""
    path = _cache_path(bas, bit)
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    payload = {
        "fonTipi": "YAT", "fonKodu": None, "aramaMetni": None, "fonTurKod": None,
        "fonGrubu": None, "sfonTurKod": None,
        "basTarih": bas.strftime("%Y%m%d"), "bitTarih": bit.strftime("%Y%m%d"),
        "basSira": 1, "bitSira": 60000, "fonTurAciklama": None, "dil": "TR", "kurucuKod": None,
    }
    print(f"  TEFAS -> {bas} .. {bit} ...", end="", flush=True)
    js = tefas_post(FON_GENEL_URL, payload)
    rows = js.get("resultList") or []
    slim = [
        {"k": r["fonKodu"], "t": r["tarih"], "f": r["fiyat"], "p": r["tedPaySayisi"],
         "ks": r.get("kisiSayisi"), "pb": r.get("portfoyBuyukluk"), "u": r.get("fonUnvan")}
        for r in rows if r.get("fiyat") is not None and r.get("tedPaySayisi") is not None
    ]
    if not slim:
        # TEFAS, basTarih+1 TAKVIM AYI'ni asan araliga BOS liste doner (hata degil).
        # Bos cevabi onbellege ALMAYIZ - yoksa hatali bos veri kalici hale gelir.
        print(" BOS cevap (aralik cok uzun olabilir) - onbellege alinmadi")
        time.sleep(2.5)
        return slim
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(slim, f)
    print(f" {len(slim)} satir")
    time.sleep(2.5)
    return slim


class Store:
    """Tum fonlarin gunluk noktalarini tek yerde tutar: {fonKodu: {date: point}}."""

    def __init__(self):
        self.data = defaultdict(dict)
        self.unvan = {}
        self._sorted = {}
        self.covered = []          # (bas, bit) araliklari

    def ingest(self, rows):
        for r in rows:
            t = datetime.strptime(r["t"], "%Y-%m-%d").date()
            self.data[r["k"]][t] = {
                "tarih": t, "fiyat": r["f"], "tedPaySayisi": r["p"],
                "kisiSayisi": r["ks"], "portfoyBuyukluk": r["pb"], "fonUnvan": r["u"],
            }
            if r["u"]:
                self.unvan[r["k"]] = r["u"]
        self._sorted.clear()

    def ensure_range(self, bas: date, bit: date, adim_gun: int = 28):
        """[bas, bit] araligini <=adim_gun'luk parcalarla cekip store'a yukler.
        adim_gun 28: TEFAS'in siniri gun degil TAKVIM AYI bazli (basTarih + 1 ay);
        30 gunluk adim Subat'i asinca API sessizce BOS liste donuyor."""
        cur = bas
        while cur <= bit:
            son = min(cur + timedelta(days=adim_gun - 1), bit)
            self.ingest(fetch_window(cur, son))
            cur = son + timedelta(days=1)
        self.covered.append((bas, bit))

    def series(self, kod):
        s = self._sorted.get(kod)
        if s is None:
            s = [self.data[kod][t] for t in sorted(self.data[kod])]
            self._sorted[kod] = s
        return s

    def series_upto(self, kod, d: date, gun: int):
        """kod'un (d-gun, d] araligindaki noktalari - LOOK-AHEAD YOK."""
        bas = d - timedelta(days=gun)
        return [p for p in self.series(kod) if bas <= p["tarih"] <= d]

    def fiyat_ilk_sonra(self, kod, d: date, max_gun: int = 12):
        """d'den SONRAKI ilk islem gununun (tarih, fiyat)'i. Valor varsayimi burada."""
        for p in self.series(kod):
            if d < p["tarih"] <= d + timedelta(days=max_gun) and p["fiyat"]:
                return p["tarih"], p["fiyat"]
        return None, None

    def fiyat_en_son(self, kod, d: date, max_gun: int = 12):
        """tarihi <= d olan SON fiyat."""
        son = None
        for p in self.series(kod):
            if p["tarih"] <= d and p["fiyat"]:
                son = (p["tarih"], p["fiyat"])
        if son and (d - son[0]).days <= max_gun:
            return son
        return None, None


# =============================================================================
# TÜR TAHMİNİ (fon_model_portfoy.py'den kopya)
# =============================================================================

TUR_ANAHTAR_KELIMELER = [
    ("KIYMETLİ MADEN", 105), ("ALTIN", 105),
    ("PARA PİYASASI", 107),
    ("KATILIM", 114),
    ("GARANTİLİ", 103), ("KORUMA AMAÇLI", 103),
    ("FON SEPETİ", 102),
    ("SERBEST", 108),
    ("HİSSE SENEDİ", 104),
    ("BORÇLANMA", 100), ("TAHVİL", 100), ("BONO", 100),
    ("KARMA", 110),
    ("DEĞİŞKEN", 101),
]
_TR_I_GEVSEK_CEVIRI = str.maketrans({"İ": "I", "ı": "I", "i": "I"})


def _tr_upper_gevsek(s: str) -> str:
    return s.translate(_TR_I_GEVSEK_CEVIRI).upper()


def guess_fon_turu(fon_unvan: str):
    if not fon_unvan:
        return None
    unvan_buyuk = _tr_upper_gevsek(fon_unvan)
    for anahtar, sfon_tur in TUR_ANAHTAR_KELIMELER:
        if _tr_upper_gevsek(anahtar) in unvan_buyuk:
            return sfon_tur
    return None


# =============================================================================
# METRİKLER (fon_model_portfoy.py'den kopya - seri artik as-of kesilmis)
# =============================================================================

def _pencere_metrikleri(series, gun_sayisi):
    son = series[-1]
    hedef_tarih = son["tarih"] - timedelta(days=gun_sayisi)
    nokta, nokta_idx = None, None
    for i, pt in enumerate(series):
        if pt["tarih"] <= hedef_tarih:
            nokta, nokta_idx = pt, i
        else:
            break
    if nokta is None or nokta_idx == len(series) - 1 or not nokta.get("fiyat"):
        return None, None, None
    getiri_pct = (son["fiyat"] - nokta["fiyat"]) / nokta["fiyat"] * 100.0
    net_akis_tl = 0.0
    for i in range(nokta_idx + 1, len(series)):
        onceki, simdiki = series[i - 1], series[i]
        net_akis_tl += (simdiki["tedPaySayisi"] - onceki["tedPaySayisi"]) * simdiki["fiyat"]
    akis_oran_pct = None
    if nokta.get("portfoyBuyukluk") and nokta["portfoyBuyukluk"] > 0:
        akis_oran_pct = net_akis_tl / nokta["portfoyBuyukluk"] * 100.0
    return getiri_pct, net_akis_tl, akis_oran_pct


def _pencere_risk_metrikleri(series, gun_sayisi):
    son_tarih = series[-1]["tarih"]
    hedef_tarih = son_tarih - timedelta(days=gun_sayisi)
    pencere = [pt for pt in series if pt["tarih"] > hedef_tarih and pt.get("fiyat")]
    if len(pencere) < 3:
        return None, None
    fiyatlar = [pt["fiyat"] for pt in pencere]
    getiriler = [(fiyatlar[i] - fiyatlar[i - 1]) / fiyatlar[i - 1]
                 for i in range(1, len(fiyatlar)) if fiyatlar[i - 1]]
    volatilite = None
    if len(getiriler) >= 2:
        ort = sum(getiriler) / len(getiriler)
        varyans = sum((g - ort) ** 2 for g in getiriler) / (len(getiriler) - 1)
        volatilite = (varyans ** 0.5) * 100.0
    tepe = fiyatlar[0]
    max_dusus = 0.0
    for f in fiyatlar:
        if f > tepe:
            tepe = f
        dusus = (f - tepe) / tepe * 100.0
        if dusus < max_dusus:
            max_dusus = dusus
    return volatilite, max_dusus


def compute_fund_metrics(series):
    if len(series) < 2:
        return None
    ilk, son = series[0], series[-1]
    if not ilk["fiyat"] or ilk["fiyat"] <= 0:
        return None

    getiri_1a, net_akis_1a, akis_oran_1a = _pencere_metrikleri(series, LOOKBACK_DAYS)
    getiri_1h, net_akis_1h, akis_oran_1h = _pencere_metrikleri(series, 7)
    getiri_1g, net_akis_1g, akis_oran_1g = _pencere_metrikleri(series, 1)

    if getiri_1a is None:
        getiri_1a = (son["fiyat"] - ilk["fiyat"]) / ilk["fiyat"] * 100.0
        net_akis_1a = 0.0
        for i in range(1, len(series)):
            net_akis_1a += (series[i]["tedPaySayisi"] - series[i - 1]["tedPaySayisi"]) * series[i]["fiyat"]
        akis_oran_1a = (net_akis_1a / ilk["portfoyBuyukluk"] * 100.0
                        if ilk.get("portfoyBuyukluk") else None)

    volatilite_1h, max_dusus_1h = _pencere_risk_metrikleri(series, 7)
    volatilite_1a, max_dusus_1a = _pencere_risk_metrikleri(series, LOOKBACK_DAYS)

    sharpe_1a = None
    if volatilite_1a is not None:
        sharpe_1a = getiri_1a / max(volatilite_1a, SHARPE_VOLATILITE_TABAN)

    yogunlasma_tl = None
    if son.get("kisiSayisi") and son["kisiSayisi"] > 0 and son.get("portfoyBuyukluk"):
        yogunlasma_tl = son["portfoyBuyukluk"] / son["kisiSayisi"]

    return {
        "fonUnvan": son.get("fonUnvan"),
        "guncel_fiyat": son["fiyat"],
        "guncel_portfoy_buyuklugu": son.get("portfoyBuyukluk"),
        "guncel_kisi_sayisi": son.get("kisiSayisi"),
        "getiri_pct": getiri_1a, "net_akis_tl": net_akis_1a, "akis_oran_pct": akis_oran_1a,
        "getiri_1g": getiri_1g, "getiri_1h": getiri_1h,
        "akis_oran_1h": akis_oran_1h, "akis_oran_1g": akis_oran_1g,
        "volatilite_1h": volatilite_1h, "max_dusus_1h": max_dusus_1h,
        "volatilite_1a": volatilite_1a, "max_dusus_1a": max_dusus_1a,
        "sharpe_1a": sharpe_1a, "yogunlasma_tl": yogunlasma_tl,
        "son_tarih": son["tarih"],
    }


def apply_risk_filter(m):
    pb, ks = m.get("guncel_portfoy_buyuklugu"), m.get("guncel_kisi_sayisi")
    if pb is not None and pb < RISK_MIN_PORTFOY_BUYUKLUK:
        return False
    if ks is not None and ks < RISK_MIN_KISI_SAYISI:
        return False
    return True


def percentile_rank(value, all_values_sorted):
    if value is None or not all_values_sorted:
        return None
    n = len(all_values_sorted)
    kucuk = sum(1 for v in all_values_sorted if v < value)
    esit = sum(1 for v in all_values_sorted if v == value)
    return (kucuk + 0.5 * esit) / n * 100.0


def compute_akis_z(gecmis_degerler, bugunku_akis, min_gozlem=AKIS_Z_MIN_GOZLEM):
    if bugunku_akis is None or len(gecmis_degerler) < min_gozlem:
        return None
    ort = sum(gecmis_degerler) / len(gecmis_degerler)
    varyans = sum((d - ort) ** 2 for d in gecmis_degerler) / (len(gecmis_degerler) - 1)
    std = varyans ** 0.5
    if std <= 1e-9:
        return None
    return (bugunku_akis - ort) / std


# =============================================================================
# AS-OF TARAMA: bir tarihte kuralın ne seçeceğini yeniden kurar
# =============================================================================

GETIRI_ALANLARI = ["getiri_1h", "getiri_pct", "getiri_3a", "getiri_6a", "getiri_1y", "sharpe_1a"]
AKIS_ALANLARI = ["akis_oran_1h", "akis_oran_pct", "akis_z"]
RISK_TERS_ALANLARI = ["volatilite_1a", "yogunlasma_tl"]
RISK_DUZ_ALANLARI = ["max_dusus_1a"]


def referans_fiyat_asof(store, kod, hedef: date):
    """TEFAS kurali: hedef islem gunu degilse EN YAKIN SONRAKI islem gunu
    (ILERIYE yuvarla), REFERANS_PENCERE_GUN gunluk pencere icinde."""
    for p in store.series(kod):
        if hedef <= p["tarih"] <= hedef + timedelta(days=REFERANS_PENCERE_GUN) and p["fiyat"]:
            return p["fiyat"]
    return None


def asof_scan(store, D: date, akis_z_kullan=True):
    """D tarihine kadarki veriyle tum fonlari puanlar. Doner: [row, ...]."""
    ham = []
    for kod in store.data:
        series = store.series_upto(kod, D, LOOKBACK_DAYS + 1)
        m = compute_fund_metrics(series)
        if m is None:
            continue
        # bayat veri: D'ye kadar 5 gunden fazla fiyat yoksa fon fiilen kapali/durmus
        if (D - m["son_tarih"]).days > 5:
            continue
        r = {"fonKodu": kod, "sfon_tur": guess_fon_turu(m.get("fonUnvan")),
             "risk_passed": apply_risk_filter(m), **m}
        ham.append(r)

    # uzun vadeli getiriler (TEFAS'in takvim ayi + ileri yuvarlama kurali)
    for etiket, ay in UZUN_VADE_DONEMLERI:
        hedef = n_ay_once(D, ay)
        alan = "getiri_pct" if etiket == "1a" else f"getiri_{etiket}"
        for r in ham:
            ref = referans_fiyat_asof(store, r["fonKodu"], hedef)
            r[alan] = (r["guncel_fiyat"] - ref) / ref * 100.0 if ref else None

    # akis z-skoru: fonun KENDI gecmisi, D'ye kadarki ham veriden yeniden kuruldu
    if akis_z_kullan:
        for r in ham:
            gecmis = []
            s_full = store.series(r["fonKodu"])
            gunler = [p["tarih"] for p in s_full if p["tarih"] < D][-AKIS_GECMIS_GUN:]
            for g in gunler:
                sub = store.series_upto(r["fonKodu"], g, LOOKBACK_DAYS + 1)
                if len(sub) < 2:
                    continue
                _, _, oran = _pencere_metrikleri(sub, LOOKBACK_DAYS)
                if oran is not None:
                    gecmis.append(oran)
            r["akis_z"] = compute_akis_z(gecmis, r.get("akis_oran_pct"))
    else:
        for r in ham:
            r["akis_z"] = None

    # tur-ici yuzdelik dilimler
    dagilim = defaultdict(lambda: defaultdict(list))
    for r in ham:
        if not r["risk_passed"]:
            continue
        for alan in GETIRI_ALANLARI + AKIS_ALANLARI + RISK_TERS_ALANLARI + RISK_DUZ_ALANLARI:
            if r.get(alan) is not None:
                dagilim[r["sfon_tur"]][alan].append(r[alan])
    for tur in dagilim:
        for alan in dagilim[tur]:
            dagilim[tur][alan].sort()

    def perc(r, alan):
        return percentile_rank(r.get(alan), dagilim.get(r["sfon_tur"], {}).get(alan, []))

    for r in ham:
        if not r["risk_passed"]:
            r["toplam_skor_ham"] = None
            continue
        a = [p for p in (perc(r, x) for x in GETIRI_ALANLARI) if p is not None]
        b = [p for p in (perc(r, x) for x in AKIS_ALANLARI) if p is not None]
        c = []
        for x in RISK_TERS_ALANLARI:
            p = perc(r, x)
            if p is not None:
                c.append(100.0 - p)
        for x in RISK_DUZ_ALANLARI:
            p = perc(r, x)
            if p is not None:
                c.append(p)
        kd = {"katman_a_getiri": (sum(a) / len(a) if a else None),
              "katman_b_akis": (sum(b) / len(b) if b else None),
              "katman_c_risk": (sum(c) / len(c) if c else None)}
        r.update(kd)
        tw = sum(KATMAN_AGIRLIKLARI[k] for k, v in kd.items() if v is not None)
        r["toplam_skor_ham"] = (sum(KATMAN_AGIRLIKLARI[k] * v for k, v in kd.items()
                                    if v is not None) / tw) if tw > 0 else None

    ham_by_tur = defaultdict(list)
    for r in ham:
        if r["risk_passed"] and r["toplam_skor_ham"] is not None:
            ham_by_tur[r["sfon_tur"]].append(r["toplam_skor_ham"])
    for t in ham_by_tur:
        ham_by_tur[t].sort()
    for r in ham:
        r["toplam_skor"] = (percentile_rank(r["toplam_skor_ham"], ham_by_tur.get(r["sfon_tur"], []))
                            if r["risk_passed"] and r["toplam_skor_ham"] is not None else None)

    # veri kalitesi uyarisi (sadece "akis orani asiri" - raporda etkisi sorulacak)
    for r in ham:
        ao = r.get("akis_oran_pct")
        r["akis_asiri"] = ao is not None and abs(ao) > 500.0
    return ham


def select_portfolio_genis(ham):
    """Secim kuralinin adaylarini SIRALI ve KESILMEMIS doner (ablasyon varyantlari
    aday havuzunu daraltip yine 6 fon doldurabilsin diye ayrildi)."""
    return sorted(
        (r for r in ham
         if r["risk_passed"]
         and r.get("toplam_skor") is not None and r["toplam_skor"] >= FON_PORTFOLIO_MIN_SKOR
         and r.get("getiri_pct") is not None and r["getiri_pct"] >= FON_PORTFOLIO_MIN_AYLIK_GETIRI
         and r.get("getiri_1h") is not None
         and r.get("net_akis_tl") is not None and r["net_akis_tl"] > 0),
        key=lambda r: -r["getiri_1h"],
    )


def select_portfolio(ham):
    """update_fon_model_portfolio()'daki secim kuralinin birebir aynisi."""
    return select_portfolio_genis(ham)[:FON_PORTFOLIO_SIZE]


# =============================================================================
# KIYAS ÖLÇÜTLERİ
# =============================================================================

def bist100_getiri(bas: date, bit: date):
    """XU100 kapanislarindan (bas, bit] getirisi %. Yahoo'dan, stdlib ile."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/XU100.IS?range=2y&interval=1d"
    cache = CACHE_DIR / "xu100.json"
    try:
        if cache.exists() and (time.time() - cache.stat().st_mtime) < 3600:
            payload = json.loads(cache.read_text())
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            payload = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(payload))
        res = payload["chart"]["result"][0]
        kapanis = {}
        for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
            if c is not None:
                kapanis[datetime.fromtimestamp(ts).date()] = c
    except Exception as e:
        print(f"  [uyari] XU100 alinamadi: {e}")
        return None
    gunler = sorted(kapanis)

    def en_son(d):
        uygun = [g for g in gunler if g <= d]
        return kapanis[uygun[-1]] if uygun else None

    a, b = en_son(bas), en_son(bit)
    return (b - a) / a * 100.0 if a and b else None


def evren_getirileri(store, karar_tarihi: date, cikis_hedefi: date, ham):
    """Karar tarihinde YATIRILABILIR olan (risk filtresini gecen, giris ve cikis
    fiyati bulunan) tum fonlarin donem getirisi. Doner: {kod: getiri_pct}."""
    out = {}
    for r in ham:
        if not r["risk_passed"]:
            continue
        gt, gp = store.fiyat_ilk_sonra(r["fonKodu"], karar_tarihi)
        ct, cp = store.fiyat_ilk_sonra(r["fonKodu"], cikis_hedefi)
        if gp and cp and gt and ct and ct > gt:
            out[r["fonKodu"]] = (cp - gp) / gp * 100.0
    return out


def rastgele_dagilim(getiriler, n=FON_PORTFOLIO_SIZE, tekrar=RASTGELE_TEKRAR, seed=RASTGELE_SEED):
    rnd = random.Random(seed)
    kodlar = list(getiriler)
    if len(kodlar) < n:
        return []
    return [sum(getiriler[k] for k in rnd.sample(kodlar, n)) / n for _ in range(tekrar)]


def yuzdelik(deger, dagilim):
    if not dagilim:
        return None
    return sum(1 for d in dagilim if d < deger) / len(dagilim) * 100.0


# =============================================================================
# 1. BÖLÜM - GERÇEKLEŞEN
# =============================================================================

def bolum_gerceklesen(store):
    with open(FON_PORTFOLIO_FILE, "r", encoding="utf-8", newline="") as f:
        holdings = list(csv.DictReader(f))
    if not holdings:
        print("fon_portfoy.csv bos.")
        return
    giris_tarihi = date.fromisoformat(holdings[0]["rebalance_date"])
    bugun = date.today()

    pay = YATIRIM_TL / len(holdings)
    print("\n" + "=" * 78)
    print("1. GERCEKLESEN - fon_portfoy.csv'deki mevcut sepet")
    print("=" * 78)
    print(f"Giris (rebalance) tarihi : {giris_tarihi}")
    print(f"Bugun                    : {bugun}   ({(bugun - giris_tarihi).days} takvim gunu)")
    print(f"Yatirim                  : {YATIRIM_TL:,.0f} TL  ({len(holdings)} fon x {pay:,.0f} TL)")
    print()
    print(f"{'Kod':<5} {'Giris fiyat':>12} {'Son fiyat':>12} {'Son tarih':>11} "
          f"{'Getiri %':>9} {'Deger TL':>14}")
    print("-" * 78)
    toplam = 0.0
    detay = []
    for h in holdings:
        kod = h["fonKodu"]
        ep = float(h["entry_price"])
        st, sp = store.fiyat_en_son(kod, bugun)
        if sp is None:
            print(f"{kod:<5} {ep:>12.6f} {'VERI YOK':>12}")
            continue
        g = (sp - ep) / ep * 100.0
        deger = pay * (1 + g / 100.0)
        toplam += deger
        detay.append((kod, g, deger, h.get("veri_uyarisi", "")))
        print(f"{kod:<5} {ep:>12.6f} {sp:>12.6f} {str(st):>11} {g:>+8.2f}% {deger:>14,.0f}")
    print("-" * 78)
    top_g = (toplam - YATIRIM_TL) / YATIRIM_TL * 100.0
    print(f"{'TOPLAM':<5} {'':>12} {'':>12} {'':>11} {top_g:>+8.2f}% {toplam:>14,.0f}")
    print(f"\n1.000.000 TL -> {toplam:,.0f} TL   (net {toplam - YATIRIM_TL:+,.0f} TL, "
          f"{(bugun - giris_tarihi).days} gunde)")

    # fon_portfoy.csv'deki entry_price, tarama gununun (09-02) KENDI fiyati. O fiyattan
    # alim pratikte mumkun degil: emir o aksam verilir, ertesi islem gunu fiyatindan
    # gerceklesir. Asagidaki hesap valor'lu (gercekci) halidir.
    toplam_v = 0.0
    eksik = False
    for h in holdings:
        vt, vp = store.fiyat_ilk_sonra(h["fonKodu"], giris_tarihi)
        _, sp = store.fiyat_en_son(h["fonKodu"], bugun)
        if not (vp and sp):
            eksik = True
            continue
        toplam_v += pay * (1 + (sp - vp) / vp)
    if not eksik:
        gv = (toplam_v - YATIRIM_TL) / YATIRIM_TL * 100.0
        print(f"Valorlu (ertesi is gunu fiyatindan alim) : {gv:+.2f}% -> {toplam_v:,.0f} TL "
              f"({toplam_v - toplam:+,.0f} TL fark)")

    bist = bist100_getiri(giris_tarihi, bugun)
    if bist is not None:
        print(f"Ayni donemde BIST 100  : {bist:+.2f}%  "
              f"(1.000.000 TL -> {YATIRIM_TL * (1 + bist / 100):,.0f} TL)")
    # ayni donemde tum fon evreni
    ham = asof_scan(store, giris_tarihi, akis_z_kullan=False)
    evren = {}
    for r in ham:
        if not r["risk_passed"]:
            continue
        _, a = store.fiyat_en_son(r["fonKodu"], giris_tarihi)
        _, b = store.fiyat_en_son(r["fonKodu"], bugun)
        if a and b:
            evren[r["fonKodu"]] = (b - a) / a * 100.0
    if evren:
        vals = sorted(evren.values())
        ort = sum(vals) / len(vals)
        print(f"Ayni donemde fon evreni: ortalama {ort:+.2f}%, medyan {statistics.median(vals):+.2f}% "
              f"({len(vals)} fon)")
        dag = rastgele_dagilim(evren)
        if dag:
            p = yuzdelik(top_g, dag)
            print(f"Rastgele 6 fon dagilimi: medyan {statistics.median(dag):+.2f}%, "
                  f"%5-%95 araligi [{sorted(dag)[int(0.05*len(dag))]:+.2f}%, "
                  f"{sorted(dag)[int(0.95*len(dag))]:+.2f}%] -> "
                  f"model portfoy {p:.1f}. yuzdelikte")
    return detay


# =============================================================================
# 2. BÖLÜM - YENİDEN KURULMUŞ (AS-OF) TEST
# =============================================================================

def bolum_asof(store, karar_tarihleri):
    print("\n" + "=" * 78)
    print("2. YENIDEN KURULMUS 1 AYLIK TEST (look-ahead yok)")
    print("=" * 78)
    print(f"Valor: karar tarihi D'ye kadarki veriyle secim, alis D+1 (sonraki ilk islem gunu) "
          f"fiyatindan,\nsatis D+{TUTMA_GUN}'dan sonraki ilk islem gunu fiyatindan. Komisyon yok.\n")

    sonuclar = []
    son_veri = max(p["tarih"] for v in store.data.values() for p in v.values())
    for D in karar_tarihleri:
        cikis_hedefi = D + timedelta(days=TUTMA_GUN)
        if cikis_hedefi >= son_veri:
            print(f"[{D}] cikis tarihi ({cikis_hedefi}) icin henuz fiyat yok "
                  f"(son veri {son_veri}), donem atlandi.")
            continue
        print(f"\n--- Karar tarihi {D} ---")
        ham = asof_scan(store, D)
        sepet = select_portfolio(ham)
        aday_sayisi = sum(1 for r in ham if r["risk_passed"]
                          and r.get("toplam_skor") is not None and r["toplam_skor"] >= FON_PORTFOLIO_MIN_SKOR
                          and r.get("getiri_pct") is not None and r["getiri_pct"] >= FON_PORTFOLIO_MIN_AYLIK_GETIRI
                          and r.get("getiri_1h") is not None
                          and r.get("net_akis_tl") is not None and r["net_akis_tl"] > 0)
        if not sepet:
            print(f"  Kural bu tarihte HIC fon secmiyor (aday havuzu {aday_sayisi}). "
                  f"Sepet bos -> 1.000.000 TL nakitte kalir (%0).")
            sonuclar.append({"D": D, "bos": True, "getiri": 0.0, "getiri_nakit": 0.0,
                             "aday": aday_sayisi, "sepet": [], "fon_getirileri": [],
                             "evren_medyan": None})
            continue

        pay = YATIRIM_TL / len(sepet)
        print(f"  Taranan fon: {len(ham)}, risk filtresini gecen: {sum(1 for r in ham if r['risk_passed'])}, "
              f"aday havuzu: {aday_sayisi}, secilen: {len(sepet)}")
        print(f"  {'Kod':<5} {'Skor':>5} {'1a%':>7} {'1h%':>7} {'MnTL':>8} {'kisi':>6} | "
              f"{'Getiri':>8} {'Deger TL':>12}  uyari")
        toplam, satirlar = 0.0, []
        for r in sepet:
            kod = r["fonKodu"]
            gt, gp = store.fiyat_ilk_sonra(kod, D)
            ct, cp = store.fiyat_ilk_sonra(kod, cikis_hedefi)
            if not (gp and cp):
                print(f"  {kod:<5} fiyat bulunamadi (giris={gp}, cikis={cp}) -> 0% sayildi")
                toplam += pay
                satirlar.append((kod, None))
                continue
            g = (cp - gp) / gp * 100.0
            deger = pay * (1 + g / 100.0)
            toplam += deger
            satirlar.append((kod, g))
            uyl = []
            if r["akis_asiri"]:
                uyl.append("AKIS ASIRI")
            # KAPASITE: 1.000.000/6 = ~167 bin TL'lik bir pozisyon, fonun kendisinin
            # birkac yuz bin TL oldugu bir yerde fiilen alinamaz. Kucuk yatirimci
            # sayisi da ayni tehlikeyi isaret eder (bkz. rapordaki STM ornegi).
            if (r.get("guncel_kisi_sayisi") or 0) < 100:
                uyl.append("AZ YATIRIMCI")
            pb_mn = (r.get("guncel_portfoy_buyuklugu") or 0) / 1e6
            if pb_mn < 50:
                uyl.append("KUCUK FON")
            print(f"  {kod:<5} {r['toplam_skor']:>5.0f} {r['getiri_pct']:>7.2f} {r['getiri_1h']:>7.2f} "
                  f"{pb_mn:>8.0f} {(r.get('guncel_kisi_sayisi') or 0):>6} | "
                  f"{g:>+7.2f}% {deger:>12,.0f}  {', '.join(uyl)}")
        port_g = (toplam - YATIRIM_TL) / YATIRIM_TL * 100.0
        print(f"  {'TOPLAM':<5} {'':>5} {'':>7} {'':>7} {'':>8} {'':>6} | "
              f"{port_g:>+7.2f}% {toplam:>12,.0f}")
        # Kural 6'dan AZ fon secerse iki makul yorum var: (a) parayi secilenlere esit
        # bol (yukaridaki), (b) her fona 1/6 koy, bos kalan kontenjani nakitte tut.
        # (b) daha muhafazakar; kural cok az fon sectiginde fark buyuyor.
        port_g_nakit = port_g * len(sepet) / FON_PORTFOLIO_SIZE
        if len(sepet) < FON_PORTFOLIO_SIZE:
            print(f"  (bos kontenjan nakitte tutulsaydi: {port_g_nakit:+.2f}% -> "
                  f"{YATIRIM_TL * (1 + port_g_nakit / 100):,.0f} TL)")

        # kiyas olcutleri
        gt0, _ = store.fiyat_ilk_sonra(sepet[0]["fonKodu"], D)
        ct0, _ = store.fiyat_ilk_sonra(sepet[0]["fonKodu"], cikis_hedefi)
        bist = bist100_getiri(gt0 or D, ct0 or cikis_hedefi)
        evren = evren_getirileri(store, D, cikis_hedefi, ham)
        vals = sorted(evren.values())
        ort = sum(vals) / len(vals) if vals else None
        dag = rastgele_dagilim(evren)
        p = yuzdelik(port_g, dag) if dag else None
        print(f"  > BIST 100: {bist:+.2f}%" if bist is not None else "  > BIST 100: veri yok")
        if vals:
            print(f"  > Fon evreni ({len(vals)} fon): ortalama {ort:+.2f}%, medyan {statistics.median(vals):+.2f}%, "
                  f"en iyi {vals[-1]:+.2f}%")
        if dag:
            sd = sorted(dag)
            print(f"  > Rastgele-6 ({RASTGELE_TEKRAR} cekilis): medyan {statistics.median(dag):+.2f}%, "
                  f"%5 {sd[int(0.05*len(sd))]:+.2f}%, %95 {sd[int(0.95*len(sd))]:+.2f}%  "
                  f"-> model portfoy {p:.1f}. yuzdelik")
        sonuclar.append({
            "D": D, "bos": False, "getiri": port_g, "getiri_nakit": port_g_nakit,
            "deger": toplam, "bist": bist,
            "evren_ort": ort, "evren_medyan": statistics.median(vals) if vals else None,
            "rnd_medyan": statistics.median(dag) if dag else None, "yuzdelik": p,
            "aday": aday_sayisi, "sepet": [r["fonKodu"] for r in sepet],
            "asiri": sum(1 for r in sepet if r["akis_asiri"]),
            "giris": gt0, "cikis": ct0, "fon_getirileri": satirlar,
        })
    return sonuclar


def ozet_tablo(sonuclar):
    print("\n" + "=" * 78)
    print("3. OZET - her donemde 1.000.000 TL")
    print("=" * 78)
    print(f"{'Karar':<12} {'Sepet':>6} {'Portfoy':>9} {'Deger TL':>12} {'BIST100':>9} "
          f"{'Fon ort.':>9} {'Rastgele':>9} {'Yuzdelik':>9}")
    print("-" * 78)
    for s in sonuclar:
        if s["bos"]:
            print(f"{str(s['D']):<12} {'BOS':>6} {'0.00':>8}% {YATIRIM_TL:>12,.0f} "
                  f"{'-':>9} {'-':>9} {'-':>9} {'-':>9}")
            continue
        def f(v):
            return f"{v:+.2f}%" if v is not None else "-"
        yz = f"{s['yuzdelik']:.0f}." if s["yuzdelik"] is not None else "-"
        print(f"{str(s['D']):<12} {len(s['sepet']):>6} {s['getiri']:>+8.2f}% {s['deger']:>12,.0f} "
              f"{f(s['bist']):>9} {f(s['evren_ort']):>9} {f(s['rnd_medyan']):>9} {yz:>9}")
    dolu = [s for s in sonuclar if not s["bos"]]
    print("-" * 78)
    if dolu:
        gs = [s["getiri"] for s in dolu]
        bs = [s["bist"] for s in dolu if s["bist"] is not None]
        es = [s["evren_ort"] for s in dolu if s["evren_ort"] is not None]
        ys = [s["yuzdelik"] for s in dolu if s["yuzdelik"] is not None]
        print(f"Ortalama donem getirisi : {sum(gs)/len(gs):+.2f}%  (n={len(gs)})")
        if bs:
            print(f"Ortalama BIST 100       : {sum(bs)/len(bs):+.2f}%")
        if es:
            print(f"Ortalama fon evreni     : {sum(es)/len(es):+.2f}%")
        if ys:
            print(f"Rastgele-6 icindeki ort. yuzdelik: {sum(ys)/len(ys):.1f}  "
                  f"(50 = sansla ayni, >50 = kural katki sagliyor)")
        kazanan = sum(1 for s in dolu if s["evren_medyan"] is not None and s["getiri"] > s["evren_medyan"])
        print(f"Fon evreni MEDYANINI geme  : {kazanan}/{len(dolu)} donem  "
              f"(medyan kullanildi: ortalama, kurusluk fiyatli birkac fonun "
              f"yuzlerce %'lik hareketiyle sisiyor)")

        # Fon bazinda: kural kac SECIMDE o donemin evren medyanini gecti? Donem
        # sayisi az oldugu icin (n=10) tek tek secimler daha kalabalik bir orneklem
        # verir - ama ayni donemin secimleri birbiriyle ILISKILI, bagimsiz degil.
        secim_ust = secim_top = 0
        secim_getiriler = []
        for s in dolu:
            if s["evren_medyan"] is None:
                continue
            for kod, g in s["fon_getirileri"]:
                if g is None:
                    continue
                secim_top += 1
                secim_getiriler.append(g)
                if g > s["evren_medyan"]:
                    secim_ust += 1
        if secim_top:
            print(f"Fon bazinda: {secim_top} secimin {secim_ust} tanesi ({secim_ust/secim_top*100:.0f}%) "
                  f"kendi doneminin evren medyanini gecti "
                  f"(sans = ~%50; secim getirisi medyani {statistics.median(secim_getiriler):+.2f}%)")
        # --- Istatistiksel anlamlilik: donem getirisi EKSI ayni donemin evren medyani.
        # Medyan referans alinir cunku ortalama, kurusluk NAV'li birkac fonun
        # yuzlerce %'lik hareketinden siseriyor.
        artik = [s["getiri"] - s["evren_medyan"] for s in dolu if s["evren_medyan"] is not None]
        if len(artik) >= 3:
            ort_a = sum(artik) / len(artik)
            sd_a = (sum((x - ort_a) ** 2 for x in artik) / (len(artik) - 1)) ** 0.5
            t = ort_a / (sd_a / len(artik) ** 0.5) if sd_a > 0 else 0.0
            print(f"\nEvren medyanina gore ARTIK getiri: ortalama {ort_a:+.2f} puan/ay, "
                  f"std {sd_a:.2f}, n={len(artik)}")
            print(f"  t-istatistigi = {t:.2f}  "
                  f"({'2.26 (n=10, %5) esigini GECIYOR' if abs(t) > 2.26 else 'anlamlilik esiginin ALTINDA'})")
            kalan = sorted(artik)[:-2]
            print(f"  en iyi 2 ay cikarilinca ortalama artik: {sum(kalan)/len(kalan):+.2f} puan/ay "
                  f"(getirinin tek-iki aya baglanip baglanmadigini gosterir)")

        carpan = nakit_carpan = 1.0
        for s in sonuclar:
            carpan *= (1 + s["getiri"] / 100.0)
            nakit_carpan *= (1 + s["getiri_nakit"] / 100.0)
        print(f"\nUst uste (zincirlenmis) {len(sonuclar)} donem, her ay yeniden secim:")
        print(f"  secilenlere esit bolunurse : 1.000.000 TL -> {YATIRIM_TL*carpan:,.0f} TL "
              f"({(carpan-1)*100:+.2f}%)")
        print(f"  bos kontenjan nakitte      : 1.000.000 TL -> {YATIRIM_TL*nakit_carpan:,.0f} TL "
              f"({(nakit_carpan-1)*100:+.2f}%)")
        bs_c = 1.0
        for s in sonuclar:
            if s.get("bist") is not None:
                bs_c *= (1 + s["bist"] / 100.0)
        print(f"  ayni aylarda BIST 100      : 1.000.000 TL -> {YATIRIM_TL*bs_c:,.0f} TL "
              f"({(bs_c-1)*100:+.2f}%)")


# =============================================================================
# 4. BÖLÜM - DOĞRULAMA, HASSASİYET, ABLASYON
# =============================================================================

def donem_getirisi(store, sepet_kodlari, D, cikis_hedefi):
    """Verilen kodlara esit bolunmus 1.000.000 TL'nin donem getirisi (%)."""
    if not sepet_kodlari:
        return None
    pay = 1.0 / len(sepet_kodlari)
    toplam = 0.0
    for kod in sepet_kodlari:
        _, gp = store.fiyat_ilk_sonra(kod, D)
        _, cp = store.fiyat_ilk_sonra(kod, cikis_hedefi)
        toplam += pay * ((cp / gp) if (gp and cp) else 1.0)
    return (toplam - 1.0) * 100.0


def dogrula_look_ahead(store, D):
    """D'ye kadarki veriyle KESILMIS bir store kurup ayni taramayi tekrarlar.
    Secilen sepet ayni cikmazsa kodda gelecege sizan bir yer var demektir."""
    kesik = Store()
    for kod, gunler in store.data.items():
        for t, p in gunler.items():
            if t <= D:
                kesik.data[kod][t] = p
    kesik.unvan = store.unvan
    tam = [r["fonKodu"] for r in select_portfolio(asof_scan(store, D))]
    kes = [r["fonKodu"] for r in select_portfolio(asof_scan(kesik, D))]
    return tam, kes, tam == kes


def bolum_hassasiyet(store, karar_tarihleri, kaydirmalar=(-2, 0, 2)):
    """Ayni ayda karar tarihini birkac gun oynatinca sonuc ne kadar degisiyor?
    Kural saglamsa sonuclar birbirine yakin olmali."""
    print("\n" + "=" * 78)
    print("4a. KARAR TARIHI HASSASIYETI (ayni ay, +/- 2 gun)")
    print("=" * 78)
    son_veri = max(p["tarih"] for v in store.data.values() for p in v.values())
    print(f"{'Ay':<12} " + " ".join(f"{f'D{k:+d}':>9}" for k in kaydirmalar) + f" {'Yayilim':>9}")
    print("-" * 78)
    yayilimlar = []
    for D0 in karar_tarihleri:
        satir, degerler = [], []
        for k in kaydirmalar:
            D = D0 + timedelta(days=k)
            ch = D + timedelta(days=TUTMA_GUN)
            if ch >= son_veri:
                satir.append("-")
                continue
            sepet = [r["fonKodu"] for r in select_portfolio(asof_scan(store, D))]
            g = donem_getirisi(store, sepet, D, ch)
            if g is None:
                satir.append("bos")
            else:
                satir.append(f"{g:+.2f}%")
                degerler.append(g)
        yay = (max(degerler) - min(degerler)) if len(degerler) > 1 else None
        if yay is not None:
            yayilimlar.append(yay)
        print(f"{str(D0):<12} " + " ".join(f"{x:>9}" for x in satir) +
              (f" {yay:>8.1f}p" if yay is not None else f" {'-':>9}"))
    if yayilimlar:
        print("-" * 78)
        print(f"Ortalama yayilim: {sum(yayilimlar)/len(yayilimlar):.1f} puan  "
              f"(en buyuk {max(yayilimlar):.1f} puan)")
        print("Yorum: yayilim donem getirilerinin buyuklugu mertebesindeyse, olculen")
        print("       performans kuraldan cok SECIM GUNUNUN SANSINA bagli demektir.")


def bolum_ablasyon(store, karar_tarihleri):
    """Kuralin hangi parcasi is yapiyor? Ayni tarama, farkli secim kurallari."""
    print("\n" + "=" * 78)
    print("4b. ABLASYON - kuralin hangi parcasi calisiyor?")
    print("=" * 78)

    def v_tam(ham):
        return select_portfolio(ham)

    def v_skorsuz(ham):
        """Skor >= 80 sarti KALDIRILDI (getiri + akis sartlari ve haftalik siralama kaldi)."""
        return sorted((r for r in ham if r["risk_passed"]
                       and r.get("getiri_pct") is not None and r["getiri_pct"] >= FON_PORTFOLIO_MIN_AYLIK_GETIRI
                       and r.get("getiri_1h") is not None
                       and r.get("net_akis_tl") is not None and r["net_akis_tl"] > 0),
                      key=lambda r: -r["getiri_1h"])[:FON_PORTFOLIO_SIZE]

    def v_sadece_1h(ham):
        """Hicbir filtre yok: sadece haftalik getirisi en yuksek 6 fon."""
        return sorted((r for r in ham if r["risk_passed"] and r.get("getiri_1h") is not None),
                      key=lambda r: -r["getiri_1h"])[:FON_PORTFOLIO_SIZE]

    def v_sadece_skor(ham):
        """Sadece Toplam Skor'a gore en yuksek 6 fon (haftalik siralama yok)."""
        return sorted((r for r in ham if r["risk_passed"] and r.get("toplam_skor") is not None),
                      key=lambda r: -r["toplam_skor"])[:FON_PORTFOLIO_SIZE]

    def v_sadece_1a(ham):
        """Sadece ~1 aylik getirisi en yuksek 6 fon (klasik momentum)."""
        return sorted((r for r in ham if r["risk_passed"] and r.get("getiri_pct") is not None),
                      key=lambda r: -r["getiri_pct"])[:FON_PORTFOLIO_SIZE]

    def v_kapasite(ham):
        """Tam kural + KAPASITE sarti: >=100 yatirimci ve >=50 Mn TL buyukluk.
        1.000.000 TL'yi 6'ya bolunce pozisyon ~167 bin TL; birkac yuz bin TL'lik,
        12 yatirimcili bir fonda bu pozisyon fiilen alinamaz (bkz. STM, 2025-11)."""
        return [r for r in select_portfolio_genis(ham)
                if (r.get("guncel_kisi_sayisi") or 0) >= 100
                and (r.get("guncel_portfoy_buyuklugu") or 0) >= 50e6][:FON_PORTFOLIO_SIZE]

    varyantlar = [("Tam kural", v_tam), ("Tam kural+kapasite", v_kapasite),
                  ("Skor sarti yok", v_skorsuz),
                  ("Sadece 1h getiri", v_sadece_1h), ("Sadece skor", v_sadece_skor),
                  ("Sadece 1a getiri", v_sadece_1a)]
    son_veri = max(p["tarih"] for v in store.data.values() for p in v.values())
    sonuc = {ad: [] for ad, _ in varyantlar}
    evren_medyanlar = []
    for D in karar_tarihleri:
        ch = D + timedelta(days=TUTMA_GUN)
        if ch >= son_veri:
            continue
        ham = asof_scan(store, D)
        ev = evren_getirileri(store, D, ch, ham)
        evren_medyanlar.append(statistics.median(ev.values()) if ev else None)
        for ad, fn in varyantlar:
            sepet = [r["fonKodu"] for r in fn(ham)]
            sonuc[ad].append(donem_getirisi(store, sepet, D, ch))

    print(f"{'Varyant':<18} {'Ort. donem':>11} {'Medyan':>9} {'En kotu':>9} "
          f"{'Zincir 1M TL':>14} {'Evreni geme':>12}")
    print("-" * 78)
    for ad, _ in varyantlar:
        g = [x for x in sonuc[ad] if x is not None]
        if not g:
            continue
        carpan = 1.0
        for x in sonuc[ad]:
            carpan *= (1 + (x or 0.0) / 100.0)
        kaz = sum(1 for x, m in zip(sonuc[ad], evren_medyanlar)
                  if x is not None and m is not None and x > m)
        print(f"{ad:<18} {sum(g)/len(g):>+10.2f}% {statistics.median(g):>+8.2f}% "
              f"{min(g):>+8.2f}% {YATIRIM_TL*carpan:>14,.0f} {kaz:>8}/{len(g):<3}")
    em = [m for m in evren_medyanlar if m is not None]
    if em:
        carpan = 1.0
        for m in em:
            carpan *= (1 + m / 100.0)
        print(f"{'(fon evreni medyan)':<18} {sum(em)/len(em):>+10.2f}% "
              f"{statistics.median(em):>+8.2f}% {min(em):>+8.2f}% {YATIRIM_TL*carpan:>14,.0f}")


def main():
    sadece_gerceklesen = "--gerceklesen" in sys.argv
    bugun = date.today()

    # Karar tarihleri: her ayin 12'si, cikis tarihi bugunden once olacak sekilde
    karar_tarihleri = []
    if not sadece_gerceklesen:
        # Her ayin 10'u; en son donemin cikisi (D+30) bugunden ONCE olmali ki
        # gerceklesen bir cikis fiyati bulunabilsin.
        d = date(2026, 8, 10)
        for _ in range(DONEM_SAYISI):
            karar_tarihleri.append(d)
            d = n_ay_once(d, 1)
        karar_tarihleri.sort()

    store = Store()
    if karar_tarihleri:
        # en erken karar tarihinden AKIS_GECMIS_GUN + LOOKBACK kadar once baslamali
        bas = karar_tarihleri[0] - timedelta(days=LOOKBACK_DAYS + AKIS_GECMIS_GUN + 20)
    else:
        bas = bugun - timedelta(days=45)
    print(f"TEFAS gunluk verisi cekiliyor / onbellekten okunuyor: {bas} .. {bugun}")
    store.ensure_range(bas, bugun)

    # uzun vade referans fiyatlari (1/3/6/12 ay once) icin gerekli DAR pencereler
    gerekli = set()
    for D in karar_tarihleri + ([date.fromisoformat(
            next(csv.DictReader(open(FON_PORTFOLIO_FILE, encoding="utf-8")))["rebalance_date"])]
            if FON_PORTFOLIO_FILE.exists() else []):
        for _, ay in UZUN_VADE_DONEMLERI:
            h = n_ay_once(D, ay)
            if h < bas:
                gerekli.add(h)
    for h in sorted(gerekli):
        store.ingest(fetch_window(h, h + timedelta(days=REFERANS_PENCERE_GUN)))
    print(f"Store: {len(store.data)} fon, "
          f"{sum(len(v) for v in store.data.values()):,} gun-nokta\n")

    bolum_gerceklesen(store)
    if sadece_gerceklesen:
        return
    sonuclar = bolum_asof(store, karar_tarihleri)
    if sonuclar:
        ozet_tablo(sonuclar)

    if "--dogrula" in sys.argv:
        print("\n" + "=" * 78)
        print("LOOK-AHEAD DOGRULAMASI (kesilmis store ile ayni sonuc cikiyor mu?)")
        print("=" * 78)
        for D in karar_tarihleri[:3]:
            tam, kes, ayni = dogrula_look_ahead(store, D)
            print(f"{D}: {'AYNI' if ayni else 'FARKLI!'}  tam={tam}  kesik={kes}")

    if "--hassasiyet" in sys.argv:
        bolum_hassasiyet(store, karar_tarihleri)
    if "--ablasyon" in sys.argv:
        bolum_ablasyon(store, karar_tarihleri)

    # Hayatta kalma yanliligi kontrolu: eski pencerede olup bugun olmayan fon var mi?
    eski = {k for k, v in store.data.items()
            if any(p["tarih"] <= karar_tarihleri[0] for p in v.values())} if karar_tarihleri else set()
    yeni = {k for k, v in store.data.items()
            if any((bugun - p["tarih"]).days <= 7 for p in v.values())}
    kaybolan = eski - yeni
    print(f"\nHayatta kalma kontrolu: ilk donemde verisi olan {len(eski)} fondan "
          f"{len(kaybolan)} tanesinin son 7 gunde verisi yok "
          f"({', '.join(sorted(kaybolan)[:12])}{'...' if len(kaybolan) > 12 else ''})")


if __name__ == "__main__":
    main()
