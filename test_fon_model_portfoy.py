#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fon_model_portfoy.py icindeki HESAPLAMA fonksiyonlari icin otomatik testler.

Amac: skorlama/veri-kalitesi mantiginda bir degisiklik yapildiginda (ornegin
bir esik degeri, bir formul, bir None-guard) bunun beklenen davranisi
BOZUP BOZMADIGINI, TEFAS'a manuel bakmadan, saniyeler icinde dogrulamak.
Daha once yasanan iki gercek hata da burada birer regresyon testi olarak
duruyor: percentile_rank'teki None karsilastirma hatasi ve skor kumelenmesi
duzeltmesi (bkz. TestPercentileRank, TestValidateDataQuality).

Calistirmak icin:
    python3 -m unittest test_fon_model_portfoy.py -v
veya:
    python3 test_fon_model_portfoy.py
"""

import unittest
from datetime import date, timedelta

from fon_model_portfoy import (
    percentile_rank,
    _pencere_metrikleri,
    _pencere_risk_metrikleri,
    compute_fund_metrics,
    apply_risk_filter,
    compute_akis_z,
    compute_skor_degisim,
    validate_data_quality,
    SHARPE_VOLATILITE_TABAN,
    AKIS_Z_MIN_GOZLEM,
    RISK_MIN_PORTFOY_BUYUKLUK,
    RISK_MIN_KISI_SAYISI,
    UYARI_ALAN_ESLEME,
)


def nokta(gun, fiyat, ted_pay, portfoy=None, kisi=None):
    """Test serilerini kisa yazmak icin yardimci: gun = 2026-01-01'den itibaren
    kacinci gun (0-indeksli)."""
    return {
        "tarih": date(2026, 1, 1) + timedelta(days=gun),
        "fiyat": fiyat,
        "tedPaySayisi": ted_pay,
        "portfoyBuyukluk": portfoy if portfoy is not None else fiyat * ted_pay,
        "kisiSayisi": kisi,
        "fonUnvan": "Test Fon",
    }


class TestPercentileRank(unittest.TestCase):
    def test_basic_siralama(self):
        degerler = [10, 20, 30, 40, 50]
        self.assertAlmostEqual(percentile_rank(30, degerler), 50.0)
        self.assertAlmostEqual(percentile_rank(10, degerler), 10.0)
        self.assertAlmostEqual(percentile_rank(50, degerler), 90.0)

    def test_esit_degerler_tie(self):
        degerler = [10, 10, 20]
        self.assertAlmostEqual(percentile_rank(10, degerler), 100 / 3, places=6)

    def test_none_ve_bos_liste_guvenli(self):
        # Bu, daha once yasanan "TypeError: '<' not supported between float ve
        # NoneType" hatasinin regresyon testidir (bkz. konusma gecmisi).
        self.assertIsNone(percentile_rank(None, [1, 2, 3]))
        self.assertIsNone(percentile_rank(5, []))
        self.assertIsNone(percentile_rank(None, []))


class TestPencereMetrikleri(unittest.TestCase):
    def test_getiri_akis_hesabi(self):
        # 10 gunluk seri: fiyat 100'den baslayip gunde +1, tedPaySayisi 1000'den
        # gunde +10, portfoyBuyukluk = fiyat * tedPaySayisi.
        series = [nokta(i, 100 + i, 1000 + i * 10) for i in range(10)]
        getiri_pct, net_akis_tl, akis_oran_pct = _pencere_metrikleri(series, 7)
        # son = gun9 (fiyat=109), hedef_tarih = gun9 - 7 = gun2 -> nokta = gun2
        # (fiyat=102, tedPaySayisi=1020, portfoyBuyukluk default'u fiyat*tedPaySayisi=104040)
        self.assertAlmostEqual(getiri_pct, (109 - 102) / 102 * 100, places=6)
        self.assertAlmostEqual(net_akis_tl, 7420.0, places=2)
        self.assertAlmostEqual(akis_oran_pct, 7420.0 / (102 * 1020) * 100, places=6)

    def test_yetersiz_gecmis_none_doner(self):
        series = [nokta(i, 100 + i, 1000) for i in range(3)]
        getiri_pct, net_akis_tl, akis_oran_pct = _pencere_metrikleri(series, 365)
        self.assertIsNone(getiri_pct)
        self.assertIsNone(net_akis_tl)
        self.assertIsNone(akis_oran_pct)


class TestPencereRiskMetrikleri(unittest.TestCase):
    def test_volatilite_ve_maks_dusus(self):
        # gun0 pencere disi kalacak sekilde kurulmus 4 noktalik seri.
        series = [
            nokta(0, 100, 1000),
            nokta(1, 110, 1000),
            nokta(2, 100, 1000),
            nokta(3, 121, 1000),
        ]
        volatilite, max_dusus = _pencere_risk_metrikleri(series, 3)
        self.assertAlmostEqual(volatilite, 21.27748587024975, places=6)
        self.assertAlmostEqual(max_dusus, -9.090909090909092, places=6)

    def test_yetersiz_pencere_none_doner(self):
        series = [nokta(0, 100, 1000), nokta(1, 101, 1000)]
        volatilite, max_dusus = _pencere_risk_metrikleri(series, 30)
        self.assertIsNone(volatilite)
        self.assertIsNone(max_dusus)


class TestComputeFundMetrics(unittest.TestCase):
    def test_temel_alanlar_dolu(self):
        series = [nokta(i, 100 + i, 1000, portfoy=100_000 + i * 1_000, kisi=500 + i)
                  for i in range(35)]
        m = compute_fund_metrics(series)
        self.assertIsNotNone(m)
        self.assertEqual(m["guncel_fiyat"], 134)
        self.assertEqual(m["kisi_degisim"], (500 + 34) - 500)
        self.assertAlmostEqual(m["yogunlasma_tl"], (100_000 + 34_000) / (500 + 34), places=6)

    def test_sharpe_taban_sifir_volatilitede_patlamiyor(self):
        # Gercekci bir kenar durum: fon birkac gun rapor verip sonra (hedef
        # tarihin hemen sonrasindan itibaren) fiyati SABIT kalmis (ör. islem
        # durmus/bayat veri). Getiri sifir degil ama risk penceresindeki
        # volatilite tam olarak 0.0 - taban (SHARPE_VOLATILITE_TABAN)
        # olmadan bu ZeroDivisionError'a yol acardi.
        series = [
            nokta(0, 100, 1000),
            nokta(1, 100, 1000),
            nokta(30, 110, 1000),
            nokta(31, 110, 1000),
            nokta(32, 110, 1000),
            nokta(33, 110, 1000),
        ]
        m = compute_fund_metrics(series)
        self.assertAlmostEqual(m["getiri_pct"], 10.0, places=6)
        self.assertAlmostEqual(m["volatilite_1a"], 0.0, places=9)
        self.assertAlmostEqual(m["sharpe_1a"], 10.0 / SHARPE_VOLATILITE_TABAN, places=2)

    def test_yetersiz_risk_penceresinde_sharpe_none(self):
        series = [nokta(0, 100, 1000), nokta(1, 105, 1000)]
        m = compute_fund_metrics(series)
        self.assertIsNotNone(m)
        self.assertIsNone(m["volatilite_1a"])
        self.assertIsNone(m["sharpe_1a"])

    def test_kisa_seri_none_doner(self):
        self.assertIsNone(compute_fund_metrics([nokta(0, 100, 1000)]))
        self.assertIsNone(compute_fund_metrics([]))


class TestApplyRiskFilter(unittest.TestCase):
    def test_gecer(self):
        passed, status = apply_risk_filter({
            "guncel_portfoy_buyuklugu": RISK_MIN_PORTFOY_BUYUKLUK * 2,
            "guncel_kisi_sayisi": RISK_MIN_KISI_SAYISI * 2,
        })
        self.assertTrue(passed)
        self.assertEqual(status, "GEÇTİ")

    def test_kucuk_portfoy_elenir(self):
        passed, status = apply_risk_filter({
            "guncel_portfoy_buyuklugu": RISK_MIN_PORTFOY_BUYUKLUK - 1,
            "guncel_kisi_sayisi": RISK_MIN_KISI_SAYISI * 2,
        })
        self.assertFalse(passed)
        self.assertIn("Portföy büyüklüğü", status)

    def test_az_yatirimci_elenir(self):
        passed, status = apply_risk_filter({
            "guncel_portfoy_buyuklugu": RISK_MIN_PORTFOY_BUYUKLUK * 2,
            "guncel_kisi_sayisi": RISK_MIN_KISI_SAYISI - 1,
        })
        self.assertFalse(passed)
        self.assertIn("Yatırımcı sayısı", status)

    def test_iki_sebep_birden(self):
        passed, status = apply_risk_filter({
            "guncel_portfoy_buyuklugu": RISK_MIN_PORTFOY_BUYUKLUK - 1,
            "guncel_kisi_sayisi": RISK_MIN_KISI_SAYISI - 1,
        })
        self.assertFalse(passed)
        self.assertIn("Portföy büyüklüğü", status)
        self.assertIn("Yatırımcı sayısı", status)


class TestComputeAkisZ(unittest.TestCase):
    def _gecmis(self, degerler, bas_gun=0):
        return [{"tarih": date(2026, 1, 1) + timedelta(days=bas_gun + i), "akis_oran_pct": v}
                for i, v in enumerate(degerler)]

    def test_z_skoru_formulu(self):
        gecmis = self._gecmis([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        z = compute_akis_z(gecmis, 15.0)
        self.assertAlmostEqual(z, 3.137746730610128, places=6)

    def test_yetersiz_gecmis_none(self):
        gecmis = self._gecmis(list(range(AKIS_Z_MIN_GOZLEM - 1)))
        self.assertIsNone(compute_akis_z(gecmis, 5.0))

    def test_sifir_varyans_none(self):
        gecmis = self._gecmis([3.0] * AKIS_Z_MIN_GOZLEM)
        self.assertIsNone(compute_akis_z(gecmis, 3.0))

    def test_bugunku_deger_yoksa_none(self):
        gecmis = self._gecmis(list(range(AKIS_Z_MIN_GOZLEM)))
        self.assertIsNone(compute_akis_z(gecmis, None))


class TestComputeSkorDegisim(unittest.TestCase):
    def setUp(self):
        self.gecmis = [
            {"tarih": date(2026, 1, 1), "toplam_skor": 50.0},
            {"tarih": date(2026, 1, 5), "toplam_skor": 55.0},
            {"tarih": date(2026, 1, 10), "toplam_skor": 60.0},
        ]

    def test_bir_gun_once(self):
        fark = compute_skor_degisim(self.gecmis, 70.0, date(2026, 1, 11), 1)
        self.assertAlmostEqual(fark, 10.0)

    def test_yedi_gun_once(self):
        fark = compute_skor_degisim(self.gecmis, 70.0, date(2026, 1, 11), 7)
        self.assertAlmostEqual(fark, 20.0)

    def test_gecmis_oncesi_tarih_none(self):
        fark = compute_skor_degisim(self.gecmis, 70.0, date(2026, 1, 11), 365)
        self.assertIsNone(fark)

    def test_bugunku_skor_none(self):
        self.assertIsNone(compute_skor_degisim(self.gecmis, None, date(2026, 1, 11), 1))

    def test_bos_gecmis_none(self):
        self.assertIsNone(compute_skor_degisim([], 70.0, date(2026, 1, 11), 1))


class TestValidateDataQuality(unittest.TestCase):
    RUN_DATE = date(2026, 8, 31)

    def _temiz_satir(self, kod="AAA"):
        return {
            "fonKodu": kod, "fonUnvan": "Temiz Fon",
            "guncel_fiyat": 10.0, "guncel_portfoy_buyuklugu": 50_000_000.0,
            "guncel_kisi_sayisi": 100, "getiri_1g": 1.0, "getiri_pct": 5.0,
            "akis_oran_pct": 10.0, "volatilite_1a": 2.0,
            "son_tarih": self.RUN_DATE.isoformat(),
        }

    def test_temiz_satirda_uyari_yok(self):
        satir = self._temiz_satir()
        ozet = validate_data_quality([satir], self.RUN_DATE)
        self.assertEqual(satir["veri_uyarilari"], [])
        self.assertEqual(ozet, [])

    def test_bariz_hatali_satir_tum_kurallari_tetikler(self):
        satir = {
            "fonKodu": "BBB", "fonUnvan": "Bozuk Fon",
            "guncel_fiyat": -1.0, "guncel_portfoy_buyuklugu": -100.0,
            "guncel_kisi_sayisi": -5, "getiri_1g": 100.0, "getiri_pct": 500.0,
            "akis_oran_pct": 1000.0, "volatilite_1a": -1.0,
            "son_tarih": (self.RUN_DATE - timedelta(days=10)).isoformat(),
        }
        ozet = validate_data_quality([satir], self.RUN_DATE)
        self.assertEqual(len(ozet), 1)
        uyarilar = " ".join(satir["veri_uyarilari"])
        self.assertEqual(len(satir["veri_uyarilari"]), 8)
        for beklenen in ["Geçersiz güncel fiyat", "Negatif portföy büyüklüğü",
                          "Negatif yatırımcı sayısı", "Günlük getiri aşırı",
                          "~1 Ay getiri aşırı", "Akış oranı aşırı",
                          "Negatif volatilite", "Veri güncel değil"]:
            self.assertIn(beklenen, uyarilar)

    def test_tekrar_eden_fon_kodu_yakalanir(self):
        satirlar = [self._temiz_satir("AAA"), self._temiz_satir("AAA")]
        validate_data_quality(satirlar, self.RUN_DATE)
        for satir in satirlar:
            self.assertIn("Fon kodu taramada birden fazla kez geçiyor", satir["veri_uyarilari"])

    def test_uyari_alan_esleme_hicbir_uyariyi_gozden_kacirmiyor(self):
        # UYARI_ALAN_ESLEME (Excel/pano hucre-vurgulama haritasi), validate_data_quality'nin
        # URETTIGI GERCEK mesajlarla senkron kalmali - biri mesaj metnini degistirip
        # esleme listesini guncellemeyi unutursa bu test kirilir.
        satir = {
            "fonKodu": "CCC", "fonUnvan": "Bozuk Fon",
            "guncel_fiyat": -1.0, "guncel_portfoy_buyuklugu": -100.0,
            "guncel_kisi_sayisi": -5, "getiri_1g": 100.0, "getiri_pct": 500.0,
            "akis_oran_pct": 1000.0, "volatilite_1a": -1.0,
            "son_tarih": (self.RUN_DATE - timedelta(days=10)).isoformat(),
        }
        validate_data_quality([satir], self.RUN_DATE)
        on_ekler = [on_ek for on_ek, _, _ in UYARI_ALAN_ESLEME]
        for uyari in satir["veri_uyarilari"]:
            self.assertTrue(any(uyari.startswith(on_ek) for on_ek in on_ekler),
                             f"'{uyari}' icin UYARI_ALAN_ESLEME'de eslesen bir on-ek yok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
