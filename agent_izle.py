#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AGENT İZLEYİCİ
===============
Claude Code'un arka planda çalıştırdığı alt-agent'ların ne yaptığını okunur
biçimde gösterir. Ham kayıtlar JSONL formatında tutulduğu için çıplak gözle
okunmuyor; bu script onları satır satır özetler.

KULLANIM
--------
    python3 agent_izle.py              # tüm agent'ları listele
    python3 agent_izle.py <id>         # bir agent'ın adımlarını göster
    python3 agent_izle.py <id> -c      # canlı izle (yeni adımlar geldikçe yaz)

<id> tam olmak zorunda değil, başının birkaç harfi yeter.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

KAYIT_DIZINI = Path.home() / ".claude/projects/-Users-alpergul"
KISALT = 160  # uzun metinler bu uzunlukta kesilir


def agent_dosyalari():
    return sorted(KAYIT_DIZINI.glob("*/subagents/agent-*.jsonl"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def kayitlari_oku(dosya):
    with dosya.open(encoding="utf-8") as f:
        for satir in f:
            satir = satir.strip()
            if satir:
                try:
                    yield json.loads(satir)
                except json.JSONDecodeError:
                    continue


def kisalt(metin, uzunluk=KISALT):
    metin = " ".join(str(metin).split())
    return metin if len(metin) <= uzunluk else metin[:uzunluk] + "…"


def adim_satiri(kayit):
    """Bir kaydı tek satırlık okunur özete çevirir; ilgisizse None döner."""
    tip = kayit.get("type")
    icerik = kayit.get("message", {}).get("content", [])
    if isinstance(icerik, str):
        icerik = [{"type": "text", "text": icerik}]
    if not isinstance(icerik, list):
        return None

    for blok in icerik:
        if not isinstance(blok, dict):
            continue
        if blok.get("type") == "text" and blok.get("text", "").strip():
            etiket = "DÜŞÜNCE" if tip == "assistant" else "GÖREV"
            return f"  {etiket}: {kisalt(blok['text'])}"
        if blok.get("type") == "tool_use":
            arac = blok.get("name", "?")
            girdi = blok.get("input", {})
            ayrinti = (girdi.get("command") or girdi.get("file_path")
                       or girdi.get("pattern") or girdi.get("description") or "")
            return f"  ARAÇ  {arac}: {kisalt(ayrinti, 110)}"
    return None


def listele():
    dosyalar = agent_dosyalari()
    if not dosyalar:
        print("Hiç agent kaydı yok.")
        return
    print(f"{'AGENT ID':<20} {'SON İŞLEM':<17} {'ADIM':>5}  İLK GÖREV")
    print("-" * 100)
    for d in dosyalar:
        agent_id = d.stem.replace("agent-", "")
        zaman = datetime.fromtimestamp(d.stat().st_mtime).strftime("%d.%m %H:%M:%S")
        kayitlar = list(kayitlari_oku(d))
        gorev = ""
        for k in kayitlar:
            ic = k.get("message", {}).get("content")
            if k.get("type") == "user" and isinstance(ic, str):
                gorev = kisalt(ic, 45)
                break
        print(f"{agent_id:<20} {zaman:<17} {len(kayitlar):>5}  {gorev}")


def goster(parca, canli=False):
    eslesen = [d for d in agent_dosyalari() if parca in d.stem]
    if not eslesen:
        print(f"'{parca}' ile eşleşen agent bulunamadı.")
        return
    dosya = eslesen[0]
    print(f"# {dosya.stem.replace('agent-', '')}\n")

    yazilan = 0
    while True:
        kayitlar = list(kayitlari_oku(dosya))
        for kayit in kayitlar[yazilan:]:
            satir = adim_satiri(kayit)
            if satir:
                print(satir, flush=True)
        yazilan = len(kayitlar)
        if not canli:
            return
        time.sleep(2)


def main():
    args = [a for a in sys.argv[1:] if a not in ("-c", "--canli")]
    canli = len(args) != len(sys.argv[1:])
    if not args:
        listele()
    else:
        goster(args[0], canli)


if __name__ == "__main__":
    main()
