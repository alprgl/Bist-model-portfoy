# tefas-search-robot

TEFAS fon tarama ve model portföy sistemi. Sadece Python standart
kütüphanesiyle yazılmış (urllib, json, csv); çıktısını GitHub Pages üzerinden
statik site olarak yayınlar.

- `fon_model_portfoy.py` — TEFAS fon tarayıcı ve model portföy üretici
- `backtest_fon.py` — kuralı geçmiş TEFAS verisiyle yeniden kurup test eder
- `test_fon_model_portfoy.py` — birim testleri

Yayın: https://alprgl.github.io/tefas-search-robot/fon-model-portfoy.html

Tasarım kararları, eşiklerin gerekçeleri ve bilinen tuzaklar için
[CLAUDE.md](CLAUDE.md) dosyasına bak.

> Bu repo eskiden `Bist-model-portfoy` adıyla BIST sinyal sistemini
> barındırıyordu; o taraf `bist_stocks_signal_robot` reposuna taşındı ve repo
> 13.09.2026'da TEFAS sistemine ayrıldı.
