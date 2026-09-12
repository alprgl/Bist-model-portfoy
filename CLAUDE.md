# bist_model_portfoy

BIST için Telegram tabanlı sinyal sistemi. Tek kullanıcılık, sadece Python
standart kütüphanesiyle yazılmış (urllib, json). Amaç yatırım tavsiyesi
üretmek değil, belirli teknik kriterlere uyan hisseleri Telegram'a bildirmek.

Bu dosyanın amacı: aylar sonra geri döndüğünde "bu eşik neden bu, bu satır
neden böyle" sorularına cevap vermek. Sadece "ne" değil "neden" yazılıyor.

## ⚠️ Bu dosyayı güncel tutma kuralı (her oturum için geçerli)

**Her işin sonunda, commit etmeden önce bu dosyayı güncelle.** Konuşma bitince
orada konuşulanlar kaybolur; kalıcı olan tek şey kod ve bu dosyadır.

Şunlar olduğunda buraya yaz:
- Bir eşik/sabit değiştiyse → yeni değeri ve **neden** değiştiğini
- Bir komut/servis eklendi, kaldırıldı veya yeniden adlandırıldıysa
- Bir tasarım kararı verildiyse → özellikle "şöyle de yapabilirdik ama şu
  yüzden yapmadık" türünden olanları
- Bir tuzak/hata keşfedildiyse → belirtisi, sebebi, çözümü
- Bir sınır öğrenildiyse (veri kaynağı, API, platform) → "Bilinen sınırlar"a

Yazma: tek seferlik düzeltmeler, yazım hataları, geçici denemeler. Dosya
şişerse okunmaz olur, o zaman işe yaramaz hale gelir.

## Parçalar

| Dosya | Görev |
|---|---|
| `supertrend_alarm.py` | Ortak kütüphane: Supertrend, RSI, hacim oranı, mum çekme, Telegram gönderimi. Diğer her şey bunu import eder. |
| `supertrend_sorgu_bot.py` | Telegram komut botu (`/analiz`, `/tara`, `/firsat`, `/haber`, `/start`, `/help`). Sürekli çalışır, komut bekler. |
| `analiz_alarm.py` | 10 dakikada bir BIST 30'u tarayıp çoklu zaman dilimi kriterine uyanı otomatik bildirir. |
| `haber_alarm.py` | Foreks RSS akışını tarar, yerel Ollama ile BIST etki analizi çıkarır. |
| `backtest.py` | Alarm kuralının geçmiş veride ne kadar işe yaradığını ölçer. |
| `agent_izle.py` | Claude Code alt-agent kayıtlarını okunur gösterir (bu projeyle ilgili değil, geliştirme yardımcısı). |
| `fon_model_portfoy.py` | TEFAS fon tarayıcı. **Ayrı bir sistem** — BIST hisse tarafıyla kod paylaşmaz, `docs/` klasörü üzerinden GitHub Pages'e otomatik commit+push yapar (`GIT_AUTO_PUBLISH`). |

## Servis durumu (launchd)

launchd ile `~/Library/LaunchAgents/com.alpergul.*.plist` üzerinden çalışırlar,
`KeepAlive` ile ayakta tutulur. **Şu an:**

- **AÇIK:** `analiz-alarm`, `supertrend-sorgu-bot`
- **KAPALI (kalıcı, kasıtlı):** `haber-alarm`, `supertrend-alarm`

Durumu kontrol et: `launchctl list | grep alpergul`

Yeniden başlat: `launchctl kickstart -k gui/501/com.alpergul.<servis>`
Durdur: `launchctl bootout gui/501/com.alpergul.<servis>`
Başlat: `launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.alpergul.<servis>.plist`

Mac uyku moduna geçerse tüm servisler durur — geri döndüğünde sinyal kaçmış
olabilir, kontrol et.

## Telegram komutları

- **`/analiz HISSE`** — tek hissenin `5dk/15dk/1s/4s/1g/1hf` zaman
  dilimlerindeki Supertrend durumunu gösterir (`TIMEFRAME_LABELS_ORDERED`).
- **`/tara`** — BIST 30'u 7 zaman diliminin (`5dk/15dk/1s/2s/4s/1g/1hf`) TAMAMINDA
  tarar; her dilim için AL yönü + RSI teyidi + yüksek hacim ayrı ayrı 1'er puan
  (dilim başına en fazla 3, toplam 21 puan), en yüksek puanlılar sıralanır.
- **`/firsat`** — 1g'de çizginin en az `%5` altında (`FIRSAT_UZAK_ESIK = -5.0`)
  sert düşmüş ama 1s'de AL'a dönmüş ve kısa vadede (5dk/15dk/1s) hacim girişi
  olan hisseleri listeler.
- **`/haber`** — son 10 haberden (`HABER_LIMIT`) şiddeti `HABER_MIN_SIDDET`
  (6) ve üzeri olanları tek tek, ayrı mesajlar halinde gönderir.
- **`/start`, `/help`** — tanıtım / komut listesi.

Güvenlik: sadece `telegram_config.json`'daki `chat_id`'den gelen komutlara
cevap verir.

## `analiz_alarm.py` sinyal kuralı (otomatik tarama)

`5dk, 15dk, 1s, 2s` zaman dilimlerinin **HEPSİNDE** aynı anda:
1. Supertrend AL yönünde,
2. RSI teyit ediyor (50-70 arası),
3. Bu dört dilimden **EN AZ BİRİNDE** hacim, son 20 mum ortalamasının
   2 katı ve mum yükselişte (`close > open`).

**Hacim şartı neden "en az biri", hepsi değil:** Hacim patlaması anlık bir
olay. 5dk ile 2s birbirinin içine geçmiş pencereler olduğu için dördünde
aynı anda 2x hacim görmek pratikte imkânsız — öyle kalsaydı sinyal hiç
çıkmazdı.

Şart sağlandığı sürece tekrar bildirmez; bozulup yeniden sağlandığında
yeniden bildirir (`analiz_alarm_state.json`).

## Gösterge parametreleri (kaynak: `supertrend_alarm.py`)

- **Supertrend:** ATR periyodu 10, çarpan 3 (`ATR_PERIOD`, `ATR_MULTIPLIER`) —
  TradingView varsayılanı. ATR, Wilder'in RMA yöntemiyle hesaplanır.
- **RSI:** periyot 14, Wilder RMA (`RSI_PERIOD`). AL yönünde teyit aralığı
  `50 < RSI < 70` (`RSI_AL_ALT=50`, `RSI_AL_UST=70`) — momentum var ama henüz
  aşırı alım değil. Araştırmalarda RSI+Supertrend en çok test edilen
  kombinasyon; bu filtre sinyallerin yaklaşık %30'unu eliyor ve elenenlerin
  ağırlıklı olarak kötü çıktığı görülüyor. SAT yönünde simetrik (`30 < RSI < 50`).
- **Hacim:** son gerçek işlem hacimli mum, önceki 20 mumun ortalamasının
  2 katını geçmeli (`volume_ratio`, eşik `>= 2.0`), ayrıca mum yükselişte olmalı.
- **Zaman dilimleri** (`TIMEFRAME_CONFIG`): `5dk, 15dk, 1s, 2s, 4s, 1g, 1hf`.
  `2s` ve `4s`, Yahoo'da yerel olarak yok — 60 dakikalık mumlardan
  `resample_ohlc` ile üretilir.
  - `2s` bilerek `TIMEFRAME_LABELS_ORDERED` içinde DEĞİL: o liste `/analiz`
    ekranını besliyor, `2s` eklenirse mevcut ekranın görünümü değişirdi.
    `2s`, sadece `analiz_alarm.py` ve `/tara` tarafından kullanılıyor.

## Kararların nedenleri (kodda yazmaz, burada dursun)

- **`HABER_MIN_SIDDET = 6`:** Bilgisayar bir gün kapalı kaldı, açılınca 87
  haber birikmiş halde Telegram'a boşaldı. Bu eşiğin altındaki haberler artık
  hiç gönderilmiyor.
- **`MIN_YONLU_HABER = 3`:** Kayıt yapısı değişince günün geçmişi sıfırlanmış,
  elde tek bir olumlu haber kalınca gün akışı "%100 olumlu" göstermişti. Hesap
  matematiksel olarak doğruydu ama örneklem 1 haberdi, yanıltıcıydı. 3'ten az
  yönlü haber varsa yüzde yerine "yeterli veri yok" yazılır.
- **Gün akışı yüzdesi adet değil PUAN ağırlıklı:** 9 puanlık tek bir iyi haber,
  3'er puanlık üç kötü haberden daha ağır basmalı (`format_gun_ozet`).
- **Neden Anthropic API değil yerel Ollama (`haber_alarm.py`):** maliyet.
  Yukarıdaki 87 haberlik birikinti API ile ciddi tutardı. Model:
  `qwen2.5:7b-instruct`, `http://localhost:11434/api/chat`'e çıplak urllib
  ile istek atılıyor, SDK yok. Ollama çalışmıyorsa analiz sessizce atlanır,
  haber yine de (filtresiz) gönderilir.
- **Neden Foreks RSS, investing.com değil:** investing.com akışında içerik
  alanı yoktu (sadece başlık+link) ve haber sayfası scraping'e karşı korumalıydı
  (403 döndü). Foreks `content:encoded` alanında gerçek özet metni veriyor.
- **Telegram HTML tuzağı:** mesajlar `parse_mode=HTML` ile gidiyor. Metne giren
  `<` karakteri Telegram tarafından etiket başlangıcı sanılıp mesaj TÜMDEN
  reddediliyor (gerçekten yaşandı — "< %2" yazınca mesajlar gitmedi).
  `html.escape` bu yüzden şart, atlanmamalı.
- **Kapanış müzayedesi mumu:** BIST'in gün sonu mumu Yahoo'da hacmi 0 olarak
  gelir (`last_traded_candle` bunu atlamak için var). Aynı mum, 4 saatlik
  yeniden örneklemede de soruna yol açmıştı: günün saatlik mum sayısı 4'ün
  katı olmadığı için günün SON mumları tamamen atılıyor, 4s Supertrend günün
  gerçek kapanışı yerine eski bir mumla hesaplanıyordu. `resample_ohlc` artık
  eksik kalan son grubu da dahil ediyor.

## Bilinen sınırlar

- Yahoo'nun günlük (`1d`) serisi bazı hisselerde 1-2 gün gecikmeli gelir;
  intraday seri daha güncel.
- 5 dakikalık veri Yahoo'da ~60 günle sınırlı (`FETCH_PLAN` — `backtest.py`);
  5dk gerektiren testler bu yüzden 60 günden geriye gidemiyor.
- **Canlı bot ile backtest uyumsuzluğu:** canlı bot 1 saatlik veriyi 1 aylık
  pencereyle çeker (`TIMEFRAME_CONFIG["1s"]`), backtest 6 aylık pencere kullanır
  (`FETCH_PLAN["1s"]`, `backtest.py`). Supertrend özyineli olduğu için kısa
  pencerenin ısınma bölgesinde iki seri farklı yön üretebilir. Canlı bot sadece
  son kapanmış muma baktığı için pratikte fazla etkilenmiyor ama bu bir
  uyumsuzluk kaynağı olarak bilinmeli. (Not: bu karşılaştırmanın tam sayıları
  önceki bir oturumda elle test edilmişti, kod içinde saklı bir kayıt değil.)
- **Backtest bulgusu — kuralın kenarı kanıtlanmış değil:** Daha önce çalıştırılan
  bir backtest'te (2026-06-25..09-11, BIST 30) mevcut alarm kuralı 95 işlem
  üretti, toplam +3.79%, maks. düşüş -1.79%. Ama toplam getirinin ~%80'i en
  iyi 3 işlemden, ~%65'i tek hisseden (TUPRS, +91%) geliyordu. En iyi 5 işlem
  çıkarılınca kalan 90 işlem net eksiydi. t-istatistiği 1.73, anlamlılık eşiği
  1.96'nın altında (bkz. `t_stat()`, `backtest.py`). 12 farklı çıkış kuralı
  denendi (`backtest.py --cikis`), hiçbiri mevcut çıkışı geçemedi. Bu tarihe
  özgü, kaydedilmiş bir çıktı değil — `backtest.py` çalıştırıldığında güncel
  veriyle farklı sonuç verebilir; bu satır o çalıştırmanın notu.
- Mac uyku moduna geçerse tüm servisler durur.

## Kurulum / bakım

**Ollama** (haber_alarm.py için, şu an kapalı ama tekrar açılırsa gerekli):
```
brew install ollama
brew services start ollama
ollama pull qwen2.5:7b-instruct
```

**Telegram ayarı:** `telegram_config.json` (bkz. `telegram_config.json.example`)
ya da `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` ortam değişkenleri. İkisi de
yoksa sinyaller sadece konsola yazılır, hata vermez.

**Backtest çalıştırma:** (iki pencere karıştırılmasın: `FETCH_PLAN` veri çekme
penceresidir — 60dk için 6 ay, Supertrend'in ısınması için geniş tutulur;
`LOOKBACK_DAYS` ise simülasyonun kapsadığı dönemdir — 90 gün.)
```
python3 backtest.py               # tam test
python3 backtest.py --detay       # tüm işlemleri listele
python3 backtest.py --dogrula     # iç tutarlılık kontrolleri
python3 backtest.py --cikis       # girişi sabit tutup çıkış varyantlarını kıyasla
```

## Kod alışkanlıkları

- Sadece stdlib (urllib, json). Ollama çağrısı da urllib ile, SDK yok.
  Yeni bağımlılık eklemeden önce sor.
- Kullanıcıya giden tüm mesajlar Türkçe. Kod içi yorumlar Türkçe + ASCII
  (ı/ğ/ş yerine i/g/s); docstring'lerde Türkçe karakter serbest.
  Bu dosya (CLAUDE.md) istisna, tam Türkçe karakterle yazıldı.
- Yorumlar "ne" değil "neden" açıklar — kod zaten ne yaptığını anlatıyor.
- Ölü kod bırakılmaz: bir özellik kaldırılınca ona ait sabit/yardım metni de gider.
- Değişiklik sonrası `python3 -m py_compile <dosya>` ile derle; servisi
  ilgilendiriyorsa `launchctl kickstart -k gui/501/com.alpergul.<servis>`.
- `.claude/agents/` altında bu projeye özel 2 alt-agent tanımlı: `kodcu`
  (özellik yazar), `backtest` (strateji test eder). Bir `dokuman` agent'ı da
  vardı, kaldırıldı: bu dosyayı güncellemek küçük ve gerekçe gerektiren bir iş,
  agent'a anlatmak yazmaktan uzun sürüyordu — koordinatörde kalması daha doğru.

**Agent'a ne zaman devredilir:** Brifing maliyeti işin kendisinden azsa. Uzun,
kendi içinde kapalı, bol çıktılı işler (örn. backtest koşusu) agent'a gider.
Bu dosyaya "şu karar şu yüzden verildi" notu düşmek gibi küçük işler
koordinatörde kalır — gerekçe zaten konuşmada, agent'a anlatmak yazmaktan uzun
sürer. Agent'ın raporu niyetini anlatır, gerçeği değil: iş bittiğinde
kod/çıktı koordinatör tarafından doğrulanır, öyle commit edilir. (Bu pratik
bugüne kadar iki hata yakaladı.)
