#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TEFAS FON MODEL PORTFÖY ARACI
==============================
Türkiye'de TEFAS'ta (Takasbank Fon Bilgilendirme Platformu) işlem gören tüm
yatırım fonlarını tarar, her fonu KENDİ TÜRÜNDEKİ (şemsiye fon türü) emsalleriyle
kıyaslayarak iki kriterde puanlar:

  Katman A - Getiri: Fonun ~1 aylık getirisi, kendi türündeki fonlara göre
             yüzdelik dilimi (percentile rank).
  Katman B - Para Akışı: Fonun tedavüldeki pay sayısındaki günlük değişimi
             (× o günün fiyatı) toplanarak net nakit girişi/çıkışı tahmin
             edilir, portföy büyüklüğüne oranlanır, yine kendi türü içinde
             yüzdelik dilimi hesaplanır. "Paranın nereye aktığı" sinyalidir.

Toplam Skor = (Getiri Percentile + Para Akışı Percentile) / 2  (0-100 arası)

VERİ KAYNAĞI
------------
TEFAS'ın herkese açık, anahtarsız dahili API'si:
  https://www.tefas.gov.tr/api/funds/fonGnlBlgSiraliGetir
Resmi/dökümente edilmiş bir API değildir (TEFAS'ın kendi web sitesinin
kullandığı uç nokta), bu yüzden TEFAS tarafında değişebilir.

KULLANIM
--------
    python3 fon_model_portfoy.py
"""

import json
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

# =============================================================================
# AYARLAR
# =============================================================================

API_BASE = "https://www.tefas.gov.tr/api/funds"
FON_GENEL_URL = f"{API_BASE}/fonGnlBlgSiraliGetir"
FON_TUR_URL = f"{API_BASE}/fonTurGetir"

OUTPUT_DIR = Path("./ciktilar")
DOCS_DIR = Path(__file__).resolve().parent / "docs"   # GitHub Pages buradan yayinlanir
GIT_AUTO_PUBLISH = True    # True ise her calistirmada docs/fon.html otomatik commit+push edilir

LOOKBACK_DAYS = 30          # getiri/akis hesabi icin kac takvim gunu geriye gidilecek
RISK_MIN_PORTFOY_BUYUKLUK = 10_000_000.0   # TL - bunun altindaki fonlar ELENIR (kucuk/likit degil)
RISK_MIN_KISI_SAYISI = 10                  # bunun altindaki fonlar ELENIR (halka acik degil / ozel fon)

REQUEST_TIMEOUT_SEC = 30
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
    "Origin": "https://www.tefas.gov.tr",
    "Referer": "https://www.tefas.gov.tr/TarihselVeriler.aspx",
}


# =============================================================================
# AĞ / VERİ ÇEKME
# =============================================================================

def tefas_post(url: str, payload: dict, retries: int = 5):
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
                wait = 3.0 * attempt
                print(f"  [uyari] 429 Too Many Requests, {wait:.0f}sn bekleniyor (deneme {attempt}/{retries})...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f"{url} alinamadi (429 tekrar denemeleri tukendi): {last_err}")


def fetch_fon_turleri():
    """Semsiye fon turlerini {sfonTuru: aciklama} olarak doner."""
    payload = tefas_post(FON_TUR_URL, {"fonTipi": "YAT", "flag": 1})
    return {t["sfonTuru"]: t["sfonTurAciklama"] for t in payload.get("resultList", [])}


TUR_ANAHTAR_KELIMELER = [
    # (fonUnvan icinde aranacak anahtar kelime, sfonTuru kodu) - ilk eslesen kazanir,
    # bu yuzden daha spesifik olanlar basta.
    ("KIYMETLİ MADEN", 105), ("ALTIN", 105),
    ("PARA PİYASASI", 107),
    ("KATILIM", 114),
    ("GARANTİLİ", 103), ("KORUMA AMAÇLI", 103),
    ("FON SEPETİ", 102),
    ("SERBEST", 108),
    ("HİSSE SENEDİ", 104), ("HİSSE SENEDI", 104),
    ("BORÇLANMA", 100), ("TAHVİL", 100), ("BONO", 100),
    ("KARMA", 110),
    ("DEĞİŞKEN", 101),
]


def guess_fon_turu(fon_unvan: str):
    """Fon unvanindaki anahtar kelimelerden semsiye fon turunu tahmin eder
    (TEFAS'in tur-bazli sorgusu rate-limit'e takildigi icin bu pratik yaklasik
    kullanilir). Eslesme yoksa None doner."""
    if not fon_unvan:
        return None
    unvan_buyuk = fon_unvan.upper()
    for anahtar, sfon_tur in TUR_ANAHTAR_KELIMELER:
        if anahtar in unvan_buyuk:
            return sfon_tur
    return None


def fetch_tum_fonlar_zaman_serisi(bas_tarih: date, bit_tarih: date):
    """Tum fonlarin bas_tarih-bit_tarih arasindaki GUNLUK gecmisini TEK istekte ceker.
    Doner: {fonKodu: [{'tarih':date, 'fiyat':float, 'tedPaySayisi':float,
                        'kisiSayisi':int, 'portfoyBuyukluk':float, 'fonUnvan':str}, ...]}
    (tarihe gore ARTAN sirali)."""
    payload = {
        "fonTipi": "YAT", "fonKodu": None, "aramaMetni": None, "fonTurKod": None,
        "fonGrubu": None, "sfonTurKod": None,
        "basTarih": bas_tarih.strftime("%Y%m%d"), "bitTarih": bit_tarih.strftime("%Y%m%d"),
        "basSira": 1, "bitSira": 60000, "fonTurAciklama": None, "dil": "TR", "kurucuKod": None,
    }
    payload_json = tefas_post(FON_GENEL_URL, payload)
    rows = payload_json.get("resultList") or []

    by_fon = defaultdict(list)
    for r in rows:
        if r.get("fiyat") is None or r.get("tedPaySayisi") is None:
            continue
        by_fon[r["fonKodu"]].append({
            "tarih": datetime.strptime(r["tarih"], "%Y-%m-%d").date(),
            "fiyat": r["fiyat"],
            "tedPaySayisi": r["tedPaySayisi"],
            "kisiSayisi": r.get("kisiSayisi"),
            "portfoyBuyukluk": r.get("portfoyBuyukluk"),
            "fonUnvan": r.get("fonUnvan"),
        })
    for fon_kodu in by_fon:
        by_fon[fon_kodu].sort(key=lambda x: x["tarih"])
    return by_fon, payload_json.get("toplamSayi")


# =============================================================================
# METRİK HESAPLAMA
# =============================================================================

def compute_fund_metrics(series):
    """series: tarihe gore artan sirali gunluk kayitlar (bkz. fetch_tum_fonlar_zaman_serisi).
    Doner: dict veya None (yetersiz veri)."""
    if len(series) < 2:
        return None

    ilk, son = series[0], series[-1]
    if not ilk["fiyat"] or ilk["fiyat"] <= 0:
        return None

    getiri_pct = (son["fiyat"] - ilk["fiyat"]) / ilk["fiyat"] * 100.0

    net_akis_tl = 0.0
    for i in range(1, len(series)):
        onceki, simdiki = series[i - 1], series[i]
        pay_degisim = simdiki["tedPaySayisi"] - onceki["tedPaySayisi"]
        net_akis_tl += pay_degisim * simdiki["fiyat"]

    akis_oran_pct = None
    if ilk.get("portfoyBuyukluk") and ilk["portfoyBuyukluk"] > 0:
        akis_oran_pct = net_akis_tl / ilk["portfoyBuyukluk"] * 100.0

    kisi_degisim = None
    if ilk.get("kisiSayisi") is not None and son.get("kisiSayisi") is not None:
        kisi_degisim = son["kisiSayisi"] - ilk["kisiSayisi"]

    return {
        "fonUnvan": son.get("fonUnvan"),
        "guncel_fiyat": son["fiyat"],
        "guncel_portfoy_buyuklugu": son.get("portfoyBuyukluk"),
        "guncel_kisi_sayisi": son.get("kisiSayisi"),
        "getiri_pct": getiri_pct,
        "net_akis_tl": net_akis_tl,
        "akis_oran_pct": akis_oran_pct,
        "kisi_degisim": kisi_degisim,
        "veri_gun_sayisi": len(series),
        "ilk_tarih": ilk["tarih"].isoformat(),
        "son_tarih": son["tarih"].isoformat(),
    }


def apply_risk_filter(metrics):
    reasons = []
    pb = metrics.get("guncel_portfoy_buyuklugu")
    ks = metrics.get("guncel_kisi_sayisi")
    if pb is not None and pb < RISK_MIN_PORTFOY_BUYUKLUK:
        reasons.append(f"Portföy büyüklüğü {pb:,.0f} TL < {RISK_MIN_PORTFOY_BUYUKLUK:,.0f} TL")
    if ks is not None and ks < RISK_MIN_KISI_SAYISI:
        reasons.append(f"Yatırımcı sayısı {ks} < {RISK_MIN_KISI_SAYISI}")
    passed = len(reasons) == 0
    return passed, "; ".join(reasons) if reasons else "GEÇTİ"


def percentile_rank(value, all_values_sorted):
    """value'nun all_values_sorted (artan sirali) icindeki yuzdelik dilimini (0-100) doner."""
    if not all_values_sorted:
        return None
    n = len(all_values_sorted)
    # kacinin value'dan kucuk oldugunu say (basit ama 2000 fon icin yeterince hizli)
    kucuk_sayisi = sum(1 for v in all_values_sorted if v < value)
    esit_sayisi = sum(1 for v in all_values_sorted if v == value)
    return (kucuk_sayisi + 0.5 * esit_sayisi) / n * 100.0


# =============================================================================
# HTML PANO (bagimsiz, tek dosyalik interaktif rapor)
# =============================================================================

DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TEFAS Fon Model Portföy</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">

<style>
  :root {
    --bg: #EEF3F1;
    --bg-elevated: #FFFFFF;
    --bg-sunken: #E3EBE8;
    --ink: #10231C;
    --ink-muted: #566B63;
    --ink-faint: #85978F;
    --border: #D3DFDA;
    --border-strong: #B9CAC2;
    --accent: #1D6E5C;
    --accent-ink: #FFFFFF;
    --accent-soft: rgba(29, 110, 92, 0.12);
    --positive: #1D7A54;
    --positive-bg: rgba(29, 122, 84, 0.11);
    --negative: #B5402C;
    --negative-bg: rgba(181, 64, 44, 0.11);
    --tier-strong-bg: rgba(29, 110, 92, 0.14);
    --tier-strong-ink: #17594A;
    --tier-watch-bg: rgba(29, 122, 84, 0.10);
    --tier-watch-ink: #1D7A54;
    --tier-weak-bg: rgba(85, 107, 99, 0.08);
    --tier-weak-ink: #566B63;
    --tier-out-bg: rgba(181, 64, 44, 0.09);
    --tier-out-ink: #B5402C;
    --shadow: 0 1px 2px rgba(16, 35, 28, 0.06), 0 8px 24px -12px rgba(16, 35, 28, 0.18);
    --font-display: 'Fraunces', Georgia, serif;
    --font-body: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    --font-mono: 'IBM Plex Mono', 'SFMono-Regular', Consolas, monospace;
  }

  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #0B1512;
      --bg-elevated: #121D19;
      --bg-sunken: #0A0F0D;
      --ink: #E7EFEC;
      --ink-muted: #93A79E;
      --ink-faint: #607068;
      --border: #223B33;
      --border-strong: #2F4E44;
      --accent: #4FC49E;
      --accent-ink: #06251D;
      --accent-soft: rgba(79, 196, 158, 0.14);
      --positive: #4FC489;
      --positive-bg: rgba(79, 196, 137, 0.13);
      --negative: #E37363;
      --negative-bg: rgba(227, 115, 99, 0.13);
      --tier-strong-bg: rgba(79, 196, 158, 0.16);
      --tier-strong-ink: #6BD8B4;
      --tier-watch-bg: rgba(79, 196, 137, 0.13);
      --tier-watch-ink: #4FC489;
      --tier-weak-bg: rgba(147, 167, 158, 0.10);
      --tier-weak-ink: #93A79E;
      --tier-out-bg: rgba(227, 115, 99, 0.12);
      --tier-out-ink: #E37363;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.3), 0 8px 24px -12px rgba(0, 0, 0, 0.5);
    }
  }

  :root[data-theme="dark"] {
    --bg: #0B1512;
    --bg-elevated: #121D19;
    --bg-sunken: #0A0F0D;
    --ink: #E7EFEC;
    --ink-muted: #93A79E;
    --ink-faint: #607068;
    --border: #223B33;
    --border-strong: #2F4E44;
    --accent: #4FC49E;
    --accent-ink: #06251D;
    --accent-soft: rgba(79, 196, 158, 0.14);
    --positive: #4FC489;
    --positive-bg: rgba(79, 196, 137, 0.13);
    --negative: #E37363;
    --negative-bg: rgba(227, 115, 99, 0.13);
    --tier-strong-bg: rgba(79, 196, 158, 0.16);
    --tier-strong-ink: #6BD8B4;
    --tier-watch-bg: rgba(79, 196, 137, 0.13);
    --tier-watch-ink: #4FC489;
    --tier-weak-bg: rgba(147, 167, 158, 0.10);
    --tier-weak-ink: #93A79E;
    --tier-out-bg: rgba(227, 115, 99, 0.12);
    --tier-out-ink: #E37363;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.3), 0 8px 24px -12px rgba(0, 0, 0, 0.5);
  }

  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: var(--font-body);
    font-size: 15px;
    line-height: 1.45;
    -webkit-font-smoothing: antialiased;
  }
  @media (prefers-reduced-motion: reduce) {
    * { animation-duration: 0.001ms !important; transition-duration: 0.001ms !important; }
  }

  .app {
    display: flex;
    flex-direction: column;
    height: 100vh;
    max-width: 1440px;
    margin: 0 auto;
    padding: 0 clamp(14px, 3vw, 32px);
  }

  .masthead {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px 24px;
    padding: 22px 2px 16px;
    border-bottom: 1px solid var(--border);
  }
  .masthead-id { display: flex; flex-direction: column; gap: 2px; }
  .eyebrow {
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.11em;
    text-transform: uppercase;
    color: var(--accent);
  }
  h1 {
    margin: 0;
    font-family: var(--font-display);
    font-weight: 600;
    font-size: clamp(22px, 3vw, 30px);
    letter-spacing: -0.01em;
    text-wrap: balance;
  }
  .masthead-meta {
    text-align: right;
    font-size: 12.5px;
    color: var(--ink-muted);
    line-height: 1.5;
  }
  .masthead-meta strong { color: var(--ink); font-weight: 600; }

  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(128px, 1fr));
    gap: 1px;
    background: var(--border);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    margin: 14px 0;
    box-shadow: var(--shadow);
  }
  .stat {
    background: var(--bg-elevated);
    padding: 12px 16px;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .stat-label {
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-faint);
    font-weight: 600;
  }
  .stat-value {
    font-family: var(--font-mono);
    font-size: 21px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
  }
  .stat-value.pos { color: var(--positive); }
  .stat-value.neg { color: var(--negative); }
  .stat-sub { font-size: 11px; color: var(--ink-muted); }

  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    padding-bottom: 12px;
  }
  .search-field {
    position: relative;
    flex: 1 1 220px;
    min-width: 160px;
  }
  .search-field input {
    width: 100%;
    padding: 8px 12px 8px 32px;
    border-radius: 8px;
    border: 1px solid var(--border-strong);
    background: var(--bg-elevated);
    color: var(--ink);
    font-family: var(--font-body);
    font-size: 13.5px;
  }
  .search-field input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .search-field::before {
    content: "";
    position: absolute;
    left: 11px; top: 50%;
    width: 12px; height: 12px;
    margin-top: -6px;
    border: 1.5px solid var(--ink-faint);
    border-radius: 50%;
    box-shadow: 5px 5px 0 -3px var(--ink-faint);
  }
  select, .toggle-chip {
    padding: 8px 12px;
    border-radius: 8px;
    border: 1px solid var(--border-strong);
    background: var(--bg-elevated);
    color: var(--ink);
    font-family: var(--font-body);
    font-size: 13px;
    cursor: pointer;
  }
  select:focus-visible, .toggle-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .toggle-chip {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    user-select: none;
    white-space: nowrap;
  }
  .toggle-chip input { accent-color: var(--accent); width: 14px; height: 14px; }
  .result-count {
    margin-left: auto;
    font-size: 12px;
    color: var(--ink-muted);
    font-variant-numeric: tabular-nums;
  }

  .table-wrap {
    flex: 1;
    min-height: 0;
    overflow: auto;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: var(--bg-elevated);
    box-shadow: var(--shadow);
  }
  table { border-collapse: collapse; width: 100%; min-width: 1080px; }
  thead th {
    position: sticky; top: 0; z-index: 2;
    background: var(--bg-sunken);
    border-bottom: 1px solid var(--border-strong);
    text-align: left;
    padding: 9px 12px;
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink-muted);
    font-weight: 600;
    white-space: nowrap;
    cursor: pointer;
  }
  thead th:hover { color: var(--ink); }
  thead th.active { color: var(--accent); }
  thead th .arrow { font-size: 9px; margin-left: 3px; opacity: 0.8; }
  thead th.num, td.num { text-align: right; }
  tbody tr.row { border-bottom: 1px solid var(--border); }
  tbody tr.row:hover { background: var(--accent-soft); }
  tbody tr.row.out { opacity: 0.55; }
  td { padding: 8px 12px; font-size: 13px; white-space: nowrap; }
  td.ticker { font-family: var(--font-mono); font-weight: 600; letter-spacing: 0.01em; }
  td.name { white-space: normal; min-width: 220px; }
  td.name .sector { display: block; font-size: 11px; color: var(--ink-faint); font-weight: 400; margin-top: 1px; }
  td.num { font-family: var(--font-mono); font-variant-numeric: tabular-nums; color: var(--ink-muted); }
  .ret.pos { color: var(--positive); }
  .ret.neg { color: var(--negative); }
  .ret.zero { color: var(--ink-faint); }

  .score-cell { display: flex; align-items: center; gap: 8px; justify-content: flex-end; }
  .score-num { font-family: var(--font-mono); font-weight: 600; font-variant-numeric: tabular-nums; width: 30px; text-align: right; }
  .score-track { width: 46px; height: 6px; border-radius: 4px; background: var(--bg-sunken); overflow: hidden; flex-shrink: 0; }
  .score-fill { height: 100%; border-radius: 4px; background: var(--ink-faint); }
  .row.strong .score-fill { background: var(--tier-strong-ink); }
  .row.strong .score-num { color: var(--tier-strong-ink); }
  .row.watch .score-fill { background: var(--tier-watch-ink); }
  .row.watch .score-num { color: var(--tier-watch-ink); }
  .row.weak .score-fill { background: var(--ink-faint); }
  .row.out .score-fill { background: var(--tier-out-ink); }

  .chip {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 20px;
    font-size: 10.5px;
    font-weight: 600;
    letter-spacing: 0.02em;
    white-space: nowrap;
  }
  .chip.strong { background: var(--tier-strong-bg); color: var(--tier-strong-ink); }
  .chip.watch { background: var(--tier-watch-bg); color: var(--tier-watch-ink); }
  .chip.weak { background: var(--tier-weak-bg); color: var(--tier-weak-ink); }
  .chip.out { background: var(--tier-out-bg); color: var(--tier-out-ink); }

  .empty-state {
    padding: 48px 20px;
    text-align: center;
    color: var(--ink-muted);
    font-size: 13.5px;
  }

  footer {
    padding: 10px 2px 16px;
    font-size: 11.5px;
    color: var(--ink-faint);
    border-top: 1px solid var(--border);
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 6px 20px;
  }

  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 6px; }

  @media (max-width: 760px) {
    .app { height: auto; min-height: 100vh; }
    .table-wrap { flex: none; }
  }
</style>
</head>
<body>
<div class="app">
  <header class="masthead">
    <div class="masthead-id">
      <span class="eyebrow">TEFAS · Fon Taraması</span>
      <h1>Fon Model Portföy</h1>
    </div>
    <div class="masthead-meta">
      Tarama tarihi: <strong id="run-date">—</strong><br>
      Kaynak: tefas.gov.tr (Takasbank)
    </div>
  </header>

  <section class="stats" id="stats"></section>

  <section class="toolbar">
    <div class="search-field">
      <input id="search" type="text" placeholder="Kod veya fon adı ara…" autocomplete="off">
    </div>
    <select id="tur-filter"></select>
    <label class="toggle-chip">
      <input type="checkbox" id="risk-toggle" checked>
      Sadece riski geçenler
    </label>
    <span class="result-count" id="result-count"></span>
  </section>

  <div class="table-wrap">
    <table>
      <thead>
        <tr id="head-row"></tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
    <div class="empty-state" id="empty-state" style="display:none;">Aramanla eşleşen fon yok.</div>
  </div>

  <footer>
    <span>Yatırım tavsiyesi değildir — sistematik bir tarama aracıdır. Karar sizindir.</span>
    <span id="fund-count-footer"></span>
  </footer>
</div>

<script>
  const DATA = __FON_DATA__;
  const RUN_DATE = "__RUN_DATE__";

  document.getElementById('run-date').textContent = RUN_DATE;
  document.getElementById('fund-count-footer').textContent = DATA.length + ' fon tarandı';

  DATA.forEach(d => { d.risk_passed = d.risk_status === 'GEÇTİ'; });

  function tierOf(d) {
    if (!d.risk_passed) return 'out';
    if (d.toplam_skor == null) return 'weak';
    if (d.toplam_skor >= 70) return 'strong';
    if (d.toplam_skor >= 40) return 'watch';
    return 'weak';
  }

  function fmtPct(v, digits) {
    if (v == null) return '<span class="ret zero">—</span>';
    const cls = v > 0.05 ? 'pos' : v < -0.05 ? 'neg' : 'zero';
    const sign = v > 0 ? '+' : '';
    return `<span class="ret ${cls}">${sign}${v.toFixed(digits == null ? 1 : digits)}%</span>`;
  }
  function fmtNum(v, digits) {
    if (v == null) return '—';
    return v.toLocaleString('tr-TR', { minimumFractionDigits: digits == null ? 2 : digits, maximumFractionDigits: digits == null ? 2 : digits });
  }

  const COLUMNS = [
    { key: 'fonKodu', label: 'Kod', sort: (d) => d.fonKodu },
    { key: 'fonUnvan', label: 'Fon Unvanı', sort: (d) => d.fonUnvan },
    { key: 'toplam_skor', label: 'Skor', num: true, sort: (d) => d.toplam_skor ?? -1 },
    { key: 'getiri_pct', label: '~1 Ay Getiri', num: true, sort: (d) => d.getiri_pct ?? -Infinity },
    { key: 'akis_oran_pct', label: 'Akış/Büyüklük', num: true, sort: (d) => d.akis_oran_pct ?? -Infinity },
    { key: 'net_akis_mn', label: 'Net Akış (Mn TL)', num: true, sort: (d) => d.net_akis_mn ?? -Infinity },
    { key: 'portfoy_buyuklugu_mn', label: 'Büyüklük (Mn TL)', num: true, sort: (d) => d.portfoy_buyuklugu_mn ?? -Infinity },
    { key: 'kisi_sayisi', label: 'Yatırımcı', num: true, sort: (d) => d.kisi_sayisi ?? -Infinity },
  ];

  let sortKey = 'toplam_skor';
  let sortDir = -1;

  const headRow = document.getElementById('head-row');
  headRow.innerHTML = COLUMNS.map(c =>
    `<th data-key="${c.key}" class="${c.num ? 'num' : ''}">${c.label}<span class="arrow"></span></th>`
  ).join('');

  headRow.querySelectorAll('th[data-key]').forEach(th => {
    th.addEventListener('click', () => {
      const key = th.dataset.key;
      if (sortKey === key) { sortDir *= -1; } else { sortKey = key; sortDir = key === 'fonKodu' || key === 'fonUnvan' ? 1 : -1; }
      render();
    });
  });

  const turSel = document.getElementById('tur-filter');
  const turler = [...new Set(DATA.map(d => d.tur).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'tr'));
  turSel.innerHTML = '<option value="">Tüm türler</option>' + turler.map(s => `<option value="${s}">${s}</option>`).join('');

  const searchInput = document.getElementById('search');
  const riskToggle = document.getElementById('risk-toggle');
  const tbody = document.getElementById('tbody');
  const emptyState = document.getElementById('empty-state');
  const resultCount = document.getElementById('result-count');

  function renderStats() {
    const passed = DATA.filter(d => d.risk_passed);
    const strong = passed.filter(d => d.toplam_skor >= 70);
    const watch = passed.filter(d => d.toplam_skor >= 40 && d.toplam_skor < 70);
    const withFlow = passed.filter(d => d.akis_oran_pct != null);
    const avgFlow = withFlow.length ? withFlow.reduce((a, d) => a + d.akis_oran_pct, 0) / withFlow.length : null;
    const top = [...passed].sort((a, b) => (b.toplam_skor ?? -1) - (a.toplam_skor ?? -1))[0];

    const tiles = [
      { label: 'Taranan Fon', value: DATA.length, sub: 'TEFAS' },
      { label: 'Riski Geçen', value: passed.length, sub: (DATA.length - passed.length) + ' elendi' },
      { label: 'Öne Çıkan', value: strong.length, sub: 'skor ≥ 70' },
      { label: 'İzlemede', value: watch.length, sub: 'skor 40–69' },
      { label: 'Ort. Akış/Büyüklük', value: avgFlow == null ? '—' : (avgFlow > 0 ? '+' : '') + avgFlow.toFixed(1) + '%', sub: '~30 günlük', cls: avgFlow > 0 ? 'pos' : avgFlow < 0 ? 'neg' : '' },
      { label: 'En Yüksek Skor', value: top ? top.fonKodu : '—', sub: top ? top.toplam_skor.toFixed(1) + ' puan' : '' },
    ];
    document.getElementById('stats').innerHTML = tiles.map(t =>
      `<div class="stat"><span class="stat-label">${t.label}</span><span class="stat-value ${t.cls || ''}">${t.value}</span><span class="stat-sub">${t.sub}</span></div>`
    ).join('');
  }

  function currentFilter() {
    const q = searchInput.value.trim().toLocaleLowerCase('tr');
    const tur = turSel.value;
    const onlyPassed = riskToggle.checked;
    return DATA.filter(d => {
      if (onlyPassed && !d.risk_passed) return false;
      if (tur && d.tur !== tur) return false;
      if (q && !(d.fonKodu.toLocaleLowerCase('tr').includes(q) || (d.fonUnvan || '').toLocaleLowerCase('tr').includes(q))) return false;
      return true;
    });
  }

  function render() {
    headRow.querySelectorAll('th[data-key]').forEach(th => {
      th.classList.toggle('active', th.dataset.key === sortKey);
      th.querySelector('.arrow').textContent = th.dataset.key === sortKey ? (sortDir === 1 ? '▲' : '▼') : '';
    });

    const col = COLUMNS.find(c => c.key === sortKey);
    const rows = currentFilter().sort((a, b) => {
      const av = col.sort(a), bv = col.sort(b);
      if (typeof av === 'string') return av.localeCompare(bv, 'tr') * sortDir;
      return (av - bv) * sortDir;
    });

    resultCount.textContent = rows.length + ' / ' + DATA.length + ' fon';
    emptyState.style.display = rows.length ? 'none' : 'block';

    tbody.innerHTML = rows.map(d => {
      const tier = tierOf(d);
      return `
      <tr class="row ${tier}">
        <td class="ticker">${d.fonKodu}</td>
        <td class="name">${d.fonUnvan || ''}<span class="sector">${d.tur || ''}</span></td>
        <td class="num"><div class="score-cell"><span class="score-num">${d.toplam_skor != null ? d.toplam_skor.toFixed(1) : '—'}</span><span class="score-track"><span class="score-fill" style="width:${Math.max(0, Math.min(100, d.toplam_skor ?? 0))}%"></span></span></div></td>
        <td class="num">${fmtPct(d.getiri_pct)}</td>
        <td class="num">${fmtPct(d.akis_oran_pct)}</td>
        <td class="num">${fmtNum(d.net_akis_mn, 1)}</td>
        <td class="num">${fmtNum(d.portfoy_buyuklugu_mn, 0)}</td>
        <td class="num">${d.kisi_sayisi != null ? d.kisi_sayisi.toLocaleString('tr-TR') : '—'}</td>
      </tr>`;
    }).join('');
  }

  searchInput.addEventListener('input', render);
  turSel.addEventListener('change', render);
  riskToggle.addEventListener('change', render);

  renderStats();
  render();
</script>
</body>
</html>
"""


def write_html_dashboard(rows, run_date_str, output_path: Path):
    """Skor + akis verisini arama/filtre/siralama ozellikli, tek dosyalik bagimsiz
    bir HTML sayfasina yazar."""
    dashboard_rows = []
    for r in rows:
        dashboard_rows.append({
            "fonKodu": r["fonKodu"], "fonUnvan": r["fonUnvan"], "tur": r["tur_aciklama"],
            "toplam_skor": r["toplam_skor"],
            "getiri_pct": r["getiri_pct"], "akis_oran_pct": r["akis_oran_pct"],
            "net_akis_mn": (r["net_akis_tl"] / 1_000_000) if r["net_akis_tl"] is not None else None,
            "portfoy_buyuklugu_mn": (r["guncel_portfoy_buyuklugu"] / 1_000_000) if r["guncel_portfoy_buyuklugu"] else None,
            "kisi_sayisi": r["guncel_kisi_sayisi"],
            "risk_status": r["risk_status"],
        })
    data_json = json.dumps(dashboard_rows, ensure_ascii=False)
    html = DASHBOARD_TEMPLATE.replace("__FON_DATA__", data_json).replace("__RUN_DATE__", run_date_str)
    output_path.write_text(html, encoding="utf-8")
    print(f"HTML panosu yazildi: {output_path}")


def git_publish(run_date_str):
    """docs/fon.html'i GitHub'a commit+push eder, boylece GitHub Pages uzerindeki
    pano otomatik guncellenir. Repo/remote/kimlik bilgisi eksikse veya push
    basarisiz olursa scripti durdurmadan uyari basar."""
    import subprocess
    repo_dir = Path(__file__).resolve().parent
    candidate_files = ["docs/fon.html"]
    files_to_add = [f for f in candidate_files if (repo_dir / f).exists()]
    try:
        subprocess.run(
            ["git", "add"] + files_to_add,
            cwd=repo_dir, check=True, capture_output=True, text=True,
        )
        commit = subprocess.run(
            ["git", "commit", "-m", f"Fon taramasi guncellemesi - {run_date_str}"],
            cwd=repo_dir, capture_output=True, text=True,
        )
        if commit.returncode != 0:
            if "nothing to commit" in commit.stdout:
                print("  Git: degisiklik yok, commit atlandi.")
            else:
                print(f"  [uyari] git commit basarisiz: {commit.stdout}{commit.stderr}")
            return
        push = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=repo_dir, capture_output=True, text=True,
        )
        if push.returncode != 0:
            print(f"  [uyari] git push basarisiz (ilk push'u elle yapman gerekebilir): {push.stderr.strip()}")
        else:
            print("  GitHub'a push edildi - Pages birkac dakika icinde guncellenir.")
    except Exception as e:
        print(f"  [uyari] Otomatik git publish basarisiz: {e}")


# =============================================================================
# EXCEL RAPORU
# =============================================================================

def write_excel_report(rows, run_date_str, output_path: Path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.formatting.rule import CellIsRule

    FONT = "Arial"
    wb = Workbook()
    ws = wb.active
    ws.title = "Fon Model Portföy"
    ws.sheet_view.showGridLines = False

    title_font = Font(name=FONT, size=14, bold=True, color="FFFFFF")
    header_font = Font(name=FONT, size=9, bold=True, color="FFFFFF")
    normal_font = Font(name=FONT, size=10)
    warn_font = Font(name=FONT, size=9, bold=True, color="FFFFFF")

    fill_navy = PatternFill("solid", fgColor="1F3864")
    fill_red = PatternFill("solid", fgColor="C00000")
    fill_header = PatternFill("solid", fgColor="2E75B6")

    thin = Side(style="thin", color="BFBFBF")
    border_all = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left = Alignment(horizontal="left", vertical="center", wrap_text=True)

    ws.merge_cells("A1:L1")
    ws["A1"] = f"TEFAS FON MODEL PORTFÖY — Tarama ({run_date_str})"
    ws["A1"].font = title_font
    ws["A1"].fill = fill_navy
    ws["A1"].alignment = center
    ws.row_dimensions[1].height = 26

    ws.merge_cells("A2:L2")
    ws["A2"] = ("UYARI: Yatırım tavsiyesi değildir, sistematik bir tarama aracıdır. "
                "Veri kaynağı: TEFAS (tefas.gov.tr). Karar sizindir.")
    ws["A2"].font = warn_font
    ws["A2"].fill = fill_red
    ws["A2"].alignment = center
    ws.row_dimensions[2].height = 20

    headers = ["Sıra", "Kod", "Fon Unvanı", "Tür", "Toplam Skor", "Getiri Percentile",
               "Akış Percentile", "~1 Ay Getiri %", "Net Akış (Mn TL)", "Akış/Büyüklük %",
               "Portföy Büyüklüğü (Mn TL)", "Yatırımcı Sayısı", "Risk Filtresi"]
    widths = [6, 8, 40, 20, 12, 14, 12, 12, 14, 14, 16, 14, 24]
    for i, (h, w) in enumerate(zip(headers, widths)):
        col = chr(ord("A") + i) if i < 26 else "A" + chr(ord("A") + i - 26)
        ws.column_dimensions[col].width = w
        c = ws.cell(row=4, column=i + 1, value=h)
        c.font = header_font
        c.fill = fill_header
        c.alignment = center
        c.border = border_all
    ws.row_dimensions[4].height = 30

    def sort_key(r):
        if not r["risk_passed"]:
            return (2, 0)
        if r["toplam_skor"] is None:
            return (1, 0)
        return (0, -r["toplam_skor"])

    rows_sorted = sorted(rows, key=sort_key)

    r_idx = 5
    for i, r in enumerate(rows_sorted):
        vals = [
            i + 1, r["fonKodu"], r["fonUnvan"], r["tur_aciklama"],
            round(r["toplam_skor"], 1) if r["toplam_skor"] is not None else None,
            round(r["getiri_percentile"], 1) if r["getiri_percentile"] is not None else None,
            round(r["akis_percentile"], 1) if r["akis_percentile"] is not None else None,
            round(r["getiri_pct"], 2) if r["getiri_pct"] is not None else None,
            round(r["net_akis_tl"] / 1_000_000, 2) if r["net_akis_tl"] is not None else None,
            round(r["akis_oran_pct"], 2) if r["akis_oran_pct"] is not None else None,
            round(r["guncel_portfoy_buyuklugu"] / 1_000_000, 1) if r["guncel_portfoy_buyuklugu"] else None,
            r["guncel_kisi_sayisi"],
            r["risk_status"],
        ]
        for col, v in enumerate(vals, 1):
            cell = ws.cell(row=r_idx, column=col, value=v)
            cell.font = normal_font
            cell.border = border_all
            cell.alignment = left if col in (3, 13) else center
        r_idx += 1

    last_row = r_idx - 1
    if last_row >= 5:
        ws.conditional_formatting.add(
            f"E5:E{last_row}",
            CellIsRule(operator="greaterThanOrEqual", formula=["70"],
                       fill=PatternFill("solid", fgColor="8EA9DB")))
        ws.conditional_formatting.add(
            f"E5:E{last_row}",
            CellIsRule(operator="between", formula=["40", "69.9"],
                       fill=PatternFill("solid", fgColor="A9D18E")))
        ws.conditional_formatting.add(
            f"M5:M{last_row}",
            CellIsRule(operator="notEqual", formula=['"GEÇTİ"'],
                       fill=PatternFill("solid", fgColor="FF7C80")))

    ws.freeze_panes = "A5"

    note_row = last_row + 3
    ws.merge_cells(f"A{note_row}:M{note_row}")
    ws[f"A{note_row}"] = (f"Not: Skorlar her fonu KENDİ TÜRÜNDEKİ emsalleriyle kıyaslar (yüzdelik dilim, "
                           f"0-100). Toplam Skor = (Getiri + Akış percentile) / 2. ~{LOOKBACK_DAYS} günlük "
                           "veri kullanılır. Net Akış, tedavüldeki pay sayısı değişiminden tahmin edilir "
                           "(gerçek TEFAS raporlarıyla küçük farklar olabilir).")
    ws[f"A{note_row}"].font = Font(name=FONT, size=9, italic=True, color="7F7F7F")
    ws[f"A{note_row}"].alignment = left

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    print(f"Excel raporu yazildi: {output_path}")


# =============================================================================
# ANA AKIŞ
# =============================================================================

def main():
    run_date = date.today()
    run_date_str = run_date.isoformat()
    print(f"=== TEFAS Fon Model Portföy Taraması — {run_date_str} ===\n")

    print("Şemsiye fon türleri çekiliyor...")
    fon_turleri = fetch_fon_turleri()
    print(f"  -> {len(fon_turleri)} tür bulundu.\n")

    bas_tarih = run_date - timedelta(days=LOOKBACK_DAYS)
    print(f"Tüm fonların {bas_tarih} - {run_date} arası zaman serisi çekiliyor (tek istek, biraz sürebilir)...")
    zaman_serisi, toplam_satir = fetch_tum_fonlar_zaman_serisi(bas_tarih, run_date)
    print(f"  -> {len(zaman_serisi)} fon, {toplam_satir} satır çekildi.\n")

    print("Metrikler hesaplanıyor...")
    ham_sonuclar = []
    for fon_kodu, series in zaman_serisi.items():
        metrics = compute_fund_metrics(series)
        if metrics is None:
            continue
        sfon_tur = guess_fon_turu(metrics.get("fonUnvan"))
        risk_passed, risk_status = apply_risk_filter(metrics)
        ham_sonuclar.append({
            "fonKodu": fon_kodu,
            "sfon_tur": sfon_tur,
            "tur_aciklama": fon_turleri.get(sfon_tur, "Bilinmiyor"),
            "risk_passed": risk_passed,
            "risk_status": risk_status,
            **metrics,
        })

    print(f"  -> {len(ham_sonuclar)} fon için metrik hesaplandı.\n")

    print("Tür-bazlı yüzdelik dilim (percentile) skorları hesaplanıyor...")
    getiri_by_tur = defaultdict(list)
    akis_by_tur = defaultdict(list)
    for r in ham_sonuclar:
        if not r["risk_passed"]:
            continue
        getiri_by_tur[r["sfon_tur"]].append(r["getiri_pct"])
        if r["akis_oran_pct"] is not None:
            akis_by_tur[r["sfon_tur"]].append(r["akis_oran_pct"])
    for tur in getiri_by_tur:
        getiri_by_tur[tur].sort()
    for tur in akis_by_tur:
        akis_by_tur[tur].sort()

    for r in ham_sonuclar:
        if not r["risk_passed"]:
            r["getiri_percentile"] = None
            r["akis_percentile"] = None
            r["toplam_skor"] = None
            continue
        r["getiri_percentile"] = percentile_rank(r["getiri_pct"], getiri_by_tur.get(r["sfon_tur"], []))
        r["akis_percentile"] = (percentile_rank(r["akis_oran_pct"], akis_by_tur.get(r["sfon_tur"], []))
                                 if r["akis_oran_pct"] is not None else None)
        parcalar = [p for p in (r["getiri_percentile"], r["akis_percentile"]) if p is not None]
        r["toplam_skor"] = sum(parcalar) / len(parcalar) if parcalar else None

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"TEFAS_Fon_Model_Portfoy_{run_date_str.replace('-', '')}.xlsx"
    write_excel_report(ham_sonuclar, run_date_str, out_path)

    html_path = OUTPUT_DIR / f"TEFAS_Fon_Model_Portfoy_{run_date_str.replace('-', '')}.html"
    write_html_dashboard(ham_sonuclar, run_date_str, html_path)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    docs_fon = DOCS_DIR / "fon.html"
    write_html_dashboard(ham_sonuclar, run_date_str, docs_fon)

    print("\n=== Tamamlandı ===")
    print(f"Excel raporu: {out_path}")
    print(f"HTML panosu: {html_path}")

    if GIT_AUTO_PUBLISH:
        print("\nGitHub'a yayinlaniyor...")
        git_publish(run_date_str)


if __name__ == "__main__":
    main()
