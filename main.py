import io
import json
import math
import os
import re
import time
import requests
import isodate
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont as RLTTFont
from reportlab.platypus import (HRFlowable, Paragraph, SimpleDocTemplate,
                                Spacer)
from google import genai
from google.genai import types

# Load keys/.env when running locally; on GitHub Actions the secrets come from the environment.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "keys", ".env"), override=True, encoding="utf-8-sig")
except ImportError:
    pass

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL")
APIFY_API_KEY = os.getenv("APIFY_API_KEY")
GEMINI_API_KEYS = [k for k in (os.getenv(f"GEMINI_API_KEY_{i}") for i in range(1, 4)) if k]
if not GEMINI_API_KEYS:
    _fallback = os.getenv("GEMINI_API_KEY")
    if _fallback:
        GEMINI_API_KEYS = [_fallback]

# Models are tried in this order. Every call starts on the first model, rotating through the
# keys; when EVERY key's free-tier daily quota on that model is spent, the run moves to the next
# model. Google counts the daily quota per model, so the same keys get a fresh allowance there.
# Override with GEMINI_MODELS="modelA,modelB,..." when Google ships a newer Flash.
MODEL_CHAIN = [m.strip() for m in os.getenv(
    "GEMINI_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash").split(",") if m.strip()]
# key_idx -> reason: key unusable on every model this run (403 API_KEY_SERVICE_BLOCKED, invalid key).
_dead_keys = {}
# (model, key_idx): that key's free-tier DAILY quota on that model is spent.
_quota_dead = set()


CHANNELS = {
    "Serdar Akinan": "@serdarakinan",
    "Erdem Atay": "@erdemataytv",
    "Veryansın Tv": "@VeryansinTv",
    "Onlar TV": "@onlartv",
    "Yılmaz Özdil": "@yilmaz_ozdil",
    "Cem Gürdeniz": "@cemgurdenizz",
    "Caspian Report": "@CaspianReport",
    "Fatih Altaylı": "@fatihaltayli",
    "Cüneyt Özdemir": "@cuneytozdemir",
    "Murat Yetkin": "@muratyetkinreport",
}

# ============================================================================
# KÖŞE YAZISI KAYNAĞI
# Dünkü yazıları çeker ve transkriptmiş gibi sisteme verir.
# GitHub Actions runner'ları UTC çalışır; saat dilimi aşağıda AÇIKÇA verildiği
# için "dün" her koşulda İstanbul takvimine göre hesaplanır.
# Cron UTC'dir: 09:00 İstanbul için workflow'da  cron: '0 6 * * *'
# ============================================================================

# Türkiye kalıcı olarak UTC+3, yaz saati uygulaması yok → sabit offset yeterli.
ISTANBUL_TZ = timezone(timedelta(hours=3))

ARTICLE_SOURCES = [
    {
        # Bu ad rapordaki "kanal" adı olur. YouTube'daki "Fatih Altaylı"
        # kanalından ayrışsın diye parantezli: aksi halde konsolidasyon
        # aşamasında video ile yazı aynı kanal sanılır.
        "name": "Fatih Altaylı (Köşe Yazısı)",
        "list_url": "https://fatihaltayli.com.tr/yazilar/fatih-altayli/kose-yazilari",
        # 1. grup = yol, 2. grup = YYYY-AA-GG tarihi. Hem mutlak hem göreli href.
        "link_pattern": r'href="(?:https?://fatihaltayli\.com\.tr)?'
                        r'(/yazarlar/fatih-altayli/(\d{4}-\d{2}-\d{2})/[^"#?\s]+)"',
    },
]

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9",
}

# Gövdeye karışmaması gereken tek satırlık arayüz metinleri (tam eşleşme).
_SKIP_LINES = {
    "köşe yazıları", "spor yazıları", "otomobil yazıları", "fatih altaylı",
    "linki kopyala", "link", "yazı içeriği", "son yazılar", "geçmiş yazılar",
    "videolar", "yazılar", "kanallar", "diğer", "sosyal medya", "tümü",
    "prev", "next", "youtube",
}

# Bu ifadelerden biri görülünce gövde bitmiştir; sonrası ilgili yazılar/footer.
_STOP_MARKERS = (
    "x'te yanıtla", "geçmiş yazılar",
    "youtube kanalına abone", "tüm hakları saklıdır",
)

# "Ağustos 3, 2026" biçimindeki tarih satırı.
_DATE_LINE_RE = re.compile(r"^[A-Za-zÇĞİÖŞÜçğıöşü]+\s+\d{1,2},\s+\d{4}$")


PROMPT_TEMPLATE = """<transkript>
{transkript}
</transkript>

<görev>
Yukarıdaki metin, {source_line}

Amaç:
Bu içeriği görmeyen biri, raporu okuduktan sonra içeriği görmüş biriyle aynı bilgiye ulaşsın. Aktarım okuyucu dostu, akılda kalıcı ve olay akışını takip eden bir anlatı olmalı — kuru bir bilgi dökümü değil. Rapora asla kendi yorumunu, çıkarımını veya yorumcunun söylemediği bir şeyi ekleme.

Yöntem:

### Aşama 1: Hikayeleri ve malzemeyi tespit et (düşünme aşamanda — çıktına yazma)
Metni baştan sona oku. Yorumcunun gündeme getirdiği diskret hikayeleri tespit et.
Bir hikaye şu bileşenlere sahip bir bütündür: aktör (kim?), eylem (ne yaptı/oldu?), bağlam (neden, hangi koşullarda?), iddia/sonuç (yorumcunun değerlendirmesi).
"Ekonomi" tek başına hikaye değildir; "Merkez bankasının 23 Mayıs'taki faiz kararı ve piyasa tepkisi" bir hikayedir.
Her hikaye için metindeki ilgili her bilgiyi topla: tüm isimler (tam ad, rol, unvan); tüm sayılar, tarihler, parasal değerler, yüzdeler; tüm doğrudan alıntılar; yorumcunun argümanı (iddia, gerekçe, karşı argüman); benzetmeler ve tarihsel/güncel referanslar; yorumcunun ileri sürdüğü sonuçlar/çıkarımlar. Bu hazırlık düşünme aşamanda kalır, çıktına yazılmaz.

### Aşama 2: Hikayeyi anlatıya dönüştür (okunabilir rapor)
Her hikayeyi şu mantıkla akıcı bir anlatıya çevir:
1. Açılış — olayı 1-2 cümlede tanımla; okuyucu kim/ne/ne zaman'ı hemen anlasın.
2. Bağlam — neden gündemde, daha önce ne oldu, ilgili aktörler nasıl konumlanıyor.
3. Gelişim — yorumcunun argümanını adım adım aktar; mantık zincirini koru ("x oldu çünkü y, buna karşılık z söylendi").
4. Detaylar — sayılar, alıntılar, isimler anlatı içine yedirilmiş şekilde. Liste değil, akıcı paragraf.
5. Sonuç — yorumcunun çıkarımı veya değerlendirmesi (varsa); çıkarımı doğrudan iddia olarak yaz, "yorumcu konumlandırıyor / değerlendiriyor" kalıbı kurma.
Tespit ettiğin hiçbir isim/sayı/alıntı/gerekçe rapora geçmeden silinmesin.
Her hikaye için ayrı bir alt başlık. Raporu <rapor> ... </rapor> arasına yaz.

Kısıtlar:
- Yorumcunun argümanını çarpıtma, basitleştirme veya yumuşatma.
- Metinde olmayan bağlam, sebep veya referans ekleme. Kendi yorumun yok.
- Dil: metnin dili (Türkçe ise Türkçe).
- Format: akıcı paragraflar. Madde işareti sadece gerçekten liste niteliğindeki bilgi için.
- Bilgi eksikliği YASAK: Kompaktlık veya akıcılık adına somut bilgi (isim, sayı, tarih, alıntı, gerekçe, argüman, neden, sonuç) ASLA atılmaz. Kısalık daha az kelime demektir, daha az bilgi değil. Şüphedeysen bilgiyi koru.
- Dil doğrudan: kanal/yorumcu adı raporun üst bilgisinde zaten var. "Yorumcu X diyor / iddia ediyor / konumlandırıyor / atıf yapıyor / savunmaktadır / belirtmektedir" kalıplarını kullanma. İddiayı doğrudan cümleye dök: "Bu indirim, yüksek faiz politikasının dönüm noktası" şeklinde. Konuşan bir başkasıysa (yorumcunun aktardığı bir kişi) o kişiyi öznede tut: "Karahan açıklamasında ... dedi". Pasif aktarım fiilleri ("belirtmektedir", "ifade etmektedir", "vurgulamaktadır", "öne sürmektedir") hiç kullanma.

Örnek — kısa bir transkript parçası ve ideal raporu:

<örnek_transkript>
Şimdi bakın, dün gece Merkez Bankası bir duyuru yaptı. Faizi 250 baz puan birden indirdiler. 50'den 47,5'e. Bu beklenmedik bir hamleydi çünkü piyasa beklentisi sadece 100 baz puan civarındaydı. Açıklamada Karahan "enflasyon eğilimi olumlu" dedi ama dolar bir gecede 35,80'den 36,20'ye fırladı. Yani piyasa karara güvenmedi. Erdoğan ise sabah konuşmasında "ekonomide normalleşme dönemi" diyerek bu indirimi savundu. Aslında bu 2024'ten beri uygulanan yüksek faiz politikasının bir dönüm noktası. Hatırlarsanız 2023'te Şimşek geldiğinde faizi 8,5'tan 50'ye çıkarmıştık.
</örnek_transkript>

<örnek_rapor>
### Merkez Bankası'nın Sürpriz Faiz İndirimi
Türkiye Cumhuriyet Merkez Bankası, dün gece politika faizini 250 baz puan birden indirerek yüzde 50'den yüzde 47,5'a düşürdü. Karar piyasa için sürpriz oldu — beklenti yalnızca 100 baz puan civarındaydı. Banka Başkanı Fatih Karahan açıklamasında "enflasyon eğilimi olumlu" ifadesini kullandı; ancak piyasa tepkisi tersine işledi: dolar/TL bir gece içinde 35,80'den 36,20'ye yükseldi, yani piyasa karara güven göstermedi. Cumhurbaşkanı Erdoğan sabah konuşmasında indirimi "ekonomide normalleşme dönemi" sözleriyle savundu. Bu indirim, 2024'ten beri sürdürülen yüksek faiz politikasının dönüm noktası; 2023'te Mehmet Şimşek'in göreve gelmesiyle faiz yüzde 8,5'tan yüzde 50'ye çıkarılmıştı.
</örnek_rapor>

Çıktı formatı:
Doğrudan <rapor> etiketiyle başla. Başka hiçbir giriş cümlesi kullanma.
</görev>"""

CONSOLIDATION_PROMPT = """<raporlar>
{combined_reports}
</raporlar>

<görev>
Yukarıda farklı kanallara ait video raporları bulunmaktadır. Bu raporları tek bir konsolide rapor haline getireceksin.

Amaç:
Raporu okuyan biri, hangi kanalın hangi olayı nasıl ele aldığını net görebilsin. Girdi raporlarındaki hiçbir somut detay (isim, sayı, tarih, alıntı, argüman, gerekçe) kaybolmasın. Anlatı akıcı ve takip edilebilir olmalı.

Önemli kural 1 — DIŞ BİLGİ YOK: Bu rapor sadece yukarıdaki girdi raporlarındaki bilgileri kullanır. Kendi eğitim verinden veya dış kaynaklardan hiçbir bilgi (kişi rolü, olay tarihi, arka plan açıklaması, tarihsel referans) eklemezsin. Girdide olmayan bir şey raporda yok — uydurma, tahmin etme, "bilineni" doldurma.

Önemli kural 2 — TEKRAR YOK, BİLGİ KAYBI YOK: Aynı bilgiyi farklı cümlelerle tekrar etme; aynı olayı önce genel sonra detaylı diye iki kere anlatma. Ama hiçbir somut bilgiyi (isim, sayı, tarih, alıntı, gerekçe, argüman, neden, sonuç) atma — her bilgi raporun BİR yerinde mutlaka geçer: ortaksa Ortak Bilgiler'de, özgünse kanal katkısında. Şüphedeysen bilgiyi koru. Kısalığı kelime tasarrufundan kazan, bilgi atarak değil.

Önemli kural 3 — ORTAK BİLGİLER BÖLÜMÜNDE ORTAK, KATKILARDA ÖZGÜN: Birden fazla kanalın PAYLAŞTIĞI bilgiler (olay özeti, ortak aktörler, ortak gerçekler, ortak alıntılar, birden çok kanalın benzer şekilde aktardığı detaylar) "Ortak Bilgiler" bölümünde BİR KEZ yazılır. Her kanalın katkısında sadece o kanala ÖZGÜ kısım kalır (özel argüman, özel iddia, başka kanalda olmayan isim/sayı/alıntı, kanala has yorum). Bu yapı sayesinde aynı bilgi raporda dört-beş kere tekrar edilmek zorunda kalmaz.

Önemli kural 4 — ÇIPLAK BİLGİ, EDEBİ ÜSLUP YASAK: Yorumcuların metaforlarını ("müthiş dalga", "siyasi rüzgar", "ayı bacağı yelken metaforu", "kasırga", "deprem", "mührünü vurdu", "damga vurdu", "yankı uyandırdı") rapora KOPYALAMA. Bunlar bilgi taşımaz, sadece üslup. Metafor görürsen metaforu at, arkasındaki somut iddiayı yaz; atarken cümleler arasındaki neden-sonuç bağını koru ("çünkü", "bu yüzden", "buna karşılık"). Süs sıfatlarını ("büyük", "olağanüstü", "müthiş", "kritik", "tarihi", "çarpıcı") nedeni belirtilmeden kullanma — sıfat varsa onu doğuran somut bilgi (sayı, oran, karşılaştırma) cümlede olmalı. Pasif aktarım fiilleri ("belirtmektedir", "ifade etmektedir", "savunmaktadır", "vurgulamaktadır", "öne sürmektedir") hiç kullanma. Aktarım fiili kullanma yeri sadece **Ortak Bilgiler** bölümüdür ve orada bile düz olur ("Akinan ve Altaylı X dedi", "Kulat ve Özdil Y iddia ediyor", "X: ..."). Kanal Katkıları ve Kanallara Özel Haberler bölümlerinde aktarım fiili hiç kullanma — kanal adı başlıkta; iddiayı doğrudan cümleye dök ("Özel miting yapacak", "İstiklal Partisi kurulacak").
Örnek dönüşüm:
Yanlış: "Özel'in mitinglerde yakaladığı büyük ivme ve meydanlarda oluşan müthiş dalga, Kılıçdaroğlu'nun konumundan bağımsız olarak yeni bir siyasi rüzgar yaratmıştır."
Doğru: "Özel mitinglerde ivme kazandı; bu ivme Kılıçdaroğlu'nun konumundan bağımsız."

Yapı ve kurallar:
Tüm raporları oku. Aynı somut olayı (örneğin sadece 'Erdoğan' kelimesi geçmesini değil, 'Erdoğan'ın X açıklaması' gibi spesifik bir olayı) birden fazla kanal işlemişse "📌 ORTAK GÜNDEM" bölümüne, sadece tek bir kanal işlemişse "🎙️ KANALLARA ÖZEL HABERLER" bölümüne ekle. ORTAK GÜNDEM içindeki konuları kapsama göre sırala: en çok kanalın işlediği konu en üstte olacak şekilde azalan sırada diz; eşitlikte sıra serbest. Birbiriyle ilgisiz konuları zorlama başlıklar altında birleştirme. Aynı kişi/kurum adının iki haberde geçmesi cluster için YETERSİZ — cluster aynı somut olayı (aynı karar, aynı açıklama, aynı eylem, aynı süreç) ele almalı. İki haber aynı kişiyi farklı bağlamlarda anıyorsa onları ayrı tut: ör. "Özel'in parti kurma stratejisi" ile "Özel'in eşit yurttaşlık açıklaması" aynı cluster'a girmez. Şüphedeysen ayrı cluster yap.

Başlık kuralı — ORTAK GÜNDEM konu başlıkları:
Başlık okuru içeriğe çekmeli ama dürüst kalmalı. İyi başlık spesifiktir ve haberin asıl gerilimini taşır: somut aktör + somut eylem + hikayedeki çarpıcı unsur (beklenmedik sonuç, çelişki, rakam, cepheleşme). Aktif fiil kullan, en önemli bilgiyi başa koy. Merak uyandırabilirsin ama başlığın işaret ettiği şey raporda net şekilde cevaplanmalı; bilgiyi saklayan kalıplar ("Bakın ne oldu", "Şok karar", "Bunu kimse beklemiyordu"), ünlem/BÜYÜK HARF vurgusu ve cevapsız soru başlığı yasak. Abartı sıfat yerine haberin kendi çarpıcı gerçeğini kullan; çarpıcılık uydurulmaz, rapordaki bilgiden gelir.
Örnek:
- Fazla düz: "Merkez Bankası Faiz Kararı"
- Clickbait: "Merkez Bankası'ndan ŞOK Karar — Piyasalar Alt Üst!"
- Doğru: "Beklenti 100 Baz Puandı: Merkez Bankası 250 İndirdi, Dolar 36'yı Aştı"

Aşağıdaki şablonu birebir kullan:

📌 ORTAK GÜNDEM

### [Spesifik Konu Başlığı]
**Ortak Bilgiler:** Birden fazla kanalın ortaklaştığı tüm bilgiler buraya — olay özeti, ortak aktörler, ortak gerçekler, ortak alıntılar, birden çok kanalda geçen detaylar BİR KEZ burada yazılır. Akıcı tek bir paragraf. Aynı durumu farklı kelimelerle iki kez tarif etme. Dil 3. şahıs aktarım: bilginin sahibi olan kanal/yorumcu cümlede geçer ("Akinan ve Altaylı X dedi", "Kulat ve Özdil Y iddia ediyor"). Hangi bilginin hangi kanallardan geldiği anlatıdan anlaşılsın; her cümleye ayrı kaynak listesi sıkıştırma. Doyurucu olsun diye dış bilgi getirme — sadece girdilerde geçenler.
**Kanal Katkıları:**
- **[Kanal Adı] / [Yorumcu]:** SADECE bu kanala özgü olan kısım — özgün argümanı, başka kanalda geçmeyen iddiaları, özel kaynakları/alıntıları, kanala has yorumu. Ortak Bilgiler'de zaten yazılmış kısımları burada tekrar yazma. Dil doğrudan: kanal adı zaten başlıkta yazıyor, "X iddia ediyor / belirtiyor / diyor" kalıbı kullanma. "Özgür Özel miting yapacak", "İstiklal Partisi kurulacak" gibi düz cümle. Aynı iddiayı iki kez yazma; iddialar arasındaki neden-sonuç ilişkisini koru. Eğer bu kanalın söyledikleri büyük ölçüde diğerleriyle aynıysa "aynı çizgide" tarzı tek satır yeterli.
- **[Diğer Kanal Adı]:** ...

---

🎙️ KANALLARA ÖZEL HABERLER

### [Kanal Adı]

**[Konu 1 Başlığı]**
Konunun bağlamı ve detayları akıcı paragraflar halinde. Yorumcunun argümanı, kullandığı isimler, sayılar, alıntılar, gerekçeler eksiksiz olsun. Dil doğrudan: kanal adı zaten başlıkta, "yorumcu iddia ediyor / belirtiyor" kalıbı kullanma. Düz cümleyle yaz.

**[Konu 2 Başlığı]**
Aynı şekilde.

### [Diğer Kanal Adı]

**[Konu Başlığı]**
Detaylar.

Önemli: Aynı kanalın birden fazla konusu varsa hepsi tek bir "### [Kanal Adı]" başlığı altında toplanır. Her konu için ayrı kanal başlığı açma — kanal başlığı bir kere, altında konu alt-başlıkları.

Kısıtlar:
- Metni çok kısa özetlere dönüştürme. Akıcı bir haber bülteni dili kullan.
- Doğrudan "📌 ORTAK GÜNDEM" ile başla. Başka hiçbir giriş cümlesi kullanma.
</görev>"""

FINAL_MERGE_PROMPT = """<batch_raporları>
{batch_reports}
</batch_raporları>

<görev>
Yukarıda kendi içinde birleştirilmiş "grup" raporları bulunmaktadır. Bunları okunabilirliği yüksek, tek bir nihai rapora dönüştüreceksin.

Amaç:
Final rapor, girdi raporlarındaki tüm somut detayları (isim, sayı, tarih, alıntı, argüman, gerekçe) eksiksiz korusun. Anlatı akıcı ve takip edilebilir olsun. Aynı olayı anlatan farklı kanalları doğru bir şekilde birleştir.

Önemli kural 1 — DIŞ BİLGİ YOK: Bu rapor sadece yukarıdaki girdi raporlarındaki bilgileri kullanır. Kendi eğitim verinden veya dış kaynaklardan hiçbir bilgi (kişi rolü, olay tarihi, arka plan açıklaması, tarihsel referans) eklemezsin. Girdide olmayan bir şey raporda yok — uydurma, tahmin etme, "bilineni" doldurma.

Önemli kural 2 — TEKRAR YOK, BİLGİ KAYBI YOK: Aynı bilgiyi farklı cümlelerle tekrar etme; aynı olayı önce genel sonra detaylı diye iki kere anlatma. Ama hiçbir somut bilgiyi (isim, sayı, tarih, alıntı, gerekçe, argüman, neden, sonuç) atma — her bilgi raporun BİR yerinde mutlaka geçer: ortaksa Ortak Bilgiler'de, özgünse kanal katkısında. Şüphedeysen bilgiyi koru. Kısalığı kelime tasarrufundan kazan, bilgi atarak değil.

Önemli kural 3 — ORTAK BİLGİLER BÖLÜMÜNDE ORTAK, KATKILARDA ÖZGÜN: Birden fazla kanalın PAYLAŞTIĞI bilgiler (olay özeti, ortak aktörler, ortak gerçekler, ortak alıntılar, birden çok kanalın benzer şekilde aktardığı detaylar) "Ortak Bilgiler" bölümünde BİR KEZ yazılır. Her kanalın katkısında sadece o kanala ÖZGÜ kısım kalır (özel argüman, özel iddia, başka kanalda olmayan isim/sayı/alıntı). Bu yapı sayesinde aynı bilgi raporda dört-beş kere tekrar edilmek zorunda kalmaz.

Önemli kural 4 — ÇIPLAK BİLGİ, EDEBİ ÜSLUP YASAK: Yorumcuların metaforlarını ("müthiş dalga", "siyasi rüzgar", "ayı bacağı yelken metaforu", "kasırga", "deprem", "mührünü vurdu", "damga vurdu", "yankı uyandırdı") rapora KOPYALAMA. Bunlar bilgi taşımaz, sadece üslup. Metafor görürsen metaforu at, arkasındaki somut iddiayı yaz; atarken cümleler arasındaki neden-sonuç bağını koru ("çünkü", "bu yüzden", "buna karşılık"). Süs sıfatlarını ("büyük", "olağanüstü", "müthiş", "kritik", "tarihi", "çarpıcı") nedeni belirtilmeden kullanma — sıfat varsa onu doğuran somut bilgi (sayı, oran, karşılaştırma) cümlede olmalı. Pasif aktarım fiilleri ("belirtmektedir", "ifade etmektedir", "savunmaktadır", "vurgulamaktadır", "öne sürmektedir") hiç kullanma. Aktarım fiili kullanma yeri sadece **Ortak Bilgiler** bölümüdür ve orada bile düz olur ("Akinan ve Altaylı X dedi", "Kulat ve Özdil Y iddia ediyor", "X: ..."). Kanal Katkıları ve Kanallara Özel Haberler bölümlerinde aktarım fiili hiç kullanma — kanal adı başlıkta; iddiayı doğrudan cümleye dök ("Özel miting yapacak", "İstiklal Partisi kurulacak").
Örnek dönüşüm:
Yanlış: "Özel'in mitinglerde yakaladığı büyük ivme ve meydanlarda oluşan müthiş dalga, Kılıçdaroğlu'nun konumundan bağımsız olarak yeni bir siyasi rüzgar yaratmıştır."
Doğru: "Özel mitinglerde ivme kazandı; bu ivme Kılıçdaroğlu'nun konumundan bağımsız."

Adımlar ve kurallar:
1. Girdi raporlarındaki tüm konuları tara. Sadece gerçekten aynı somut haberi/olayı anlatan konuları nihai raporun "📌 ORTAK GÜNDEM" kısmında birleştir. Zorlama soyut başlıklar (örn. "Devletin Rolü") yaratma. Aynı kişi/kurum adının iki haberde geçmesi cluster için YETERSİZ — cluster aynı somut olayı (aynı karar, aynı açıklama, aynı eylem) ele almalı. İki haber aynı kişiyi farklı bağlamlarda anıyorsa onları ayrı tut. Şüphedeysen ayrı cluster yap. ORTAK GÜNDEM içindeki konuları kapsama göre sırala: en çok kanalın işlediği konu en üstte olacak şekilde azalan sırada diz; eşitlikte sıra serbest.
2. Sadece tek bir kanalın bahsettiği konuları "🎙️ KANALLARA ÖZEL HABERLER" kısmında topla.
3. Girdilerdeki hiçbir bağlamı, açıklayıcı sebebi, detaylı ifadeyi, isim, sayı, alıntı veya argümanı silme. Birleştirirken yoğunlaştır ama bilgi atma.
4. Başlık kuralı — ORTAK GÜNDEM konu başlıkları: Başlık okuru içeriğe çekmeli ama dürüst kalmalı. İyi başlık spesifiktir ve haberin asıl gerilimini taşır: somut aktör + somut eylem + hikayedeki çarpıcı unsur (beklenmedik sonuç, çelişki, rakam, cepheleşme). Aktif fiil kullan, en önemli bilgiyi başa koy. Merak uyandırabilirsin ama başlığın işaret ettiği şey raporda net şekilde cevaplanmalı; bilgiyi saklayan kalıplar ("Bakın ne oldu", "Şok karar", "Bunu kimse beklemiyordu"), ünlem/BÜYÜK HARF vurgusu ve cevapsız soru başlığı yasak. Abartı sıfat yerine haberin kendi çarpıcı gerçeğini kullan; çarpıcılık uydurulmaz, rapordaki bilgiden gelir.
   Örnek — Fazla düz: "Merkez Bankası Faiz Kararı" / Clickbait: "Merkez Bankası'ndan ŞOK Karar — Piyasalar Alt Üst!" / Doğru: "Beklenti 100 Baz Puandı: Merkez Bankası 250 İndirdi, Dolar 36'yı Aştı"

Çıktı şablonu:

📌 ORTAK GÜNDEM

### [Spesifik Konu Başlığı]
**Ortak Bilgiler:** Birden fazla kanalın ortaklaştığı tüm bilgiler buraya — olay özeti, ortak aktörler, ortak gerçekler, ortak alıntılar, birden çok kanalda geçen detaylar BİR KEZ burada yazılır. Akıcı tek bir paragraf. Aynı durumu farklı kelimelerle iki kez tarif etme. Dil 3. şahıs aktarım: bilginin sahibi olan kanal/yorumcu cümlede geçer ("Akinan ve Altaylı X dedi", "Kulat ve Özdil Y iddia ediyor"). Hangi bilginin hangi kanallardan geldiği anlatıdan anlaşılsın; her cümleye ayrı kaynak listesi sıkıştırma. Sadece girdilerde geçenler — dış bilgi getirme.
**Kanal Katkıları:**
- **[Kanal Adı]:** SADECE bu kanala özgü olan kısım — özgün argümanı, başka kanalda geçmeyen iddiaları, özel kaynakları/alıntıları. Ortak Bilgiler'de zaten yazılmış kısımları burada tekrar yazma. Dil doğrudan: kanal adı zaten başlıkta yazıyor, "X iddia ediyor / belirtiyor / diyor" kalıbı kullanma. "Özgür Özel miting yapacak", "İstiklal Partisi kurulacak" gibi düz cümle. Aynı iddiayı iki kez yazma; iddialar arasındaki neden-sonuç ilişkisini koru. Eğer bu kanalın söyledikleri büyük ölçüde diğerleriyle aynıysa "aynı çizgide" tarzı tek satır yeterli.

---

🎙️ KANALLARA ÖZEL HABERLER

### [Kanal Adı]

**[Konu 1 Başlığı]**
İlgili detaylar bağlamıyla birlikte akıcı paragraflar halinde. Dil doğrudan: kanal adı zaten başlıkta, "yorumcu iddia ediyor / belirtiyor" kalıbı kullanma. Düz cümleyle yaz.

**[Konu 2 Başlığı]**
Aynı şekilde.

### [Diğer Kanal Adı]

**[Konu Başlığı]**
Detaylar.

Önemli: Aynı kanalın birden fazla konusu varsa hepsi tek bir "### [Kanal Adı]" başlığı altında toplanır. Her konu için ayrı kanal başlığı açma.

Kısıtlar:
- Doğrudan "📌 ORTAK GÜNDEM" ile başla. Akıcı bir haber bülteni dili kullan.
</görev>"""


# Filtre kriterleri: içerik raporu güncel gündem haberlerine odaklı mı?
# Tutucu bias — tereddütteyse TUT, sadece bariz olarak gündem dışı içerik ATILSIN.
INTERESTS = """
Filtreleme kriterleri: içerik raporu (video veya köşe yazısı) güncel gündem haberlerine odaklı mı?

Tut (güncel gündem):
- Belirli, somut güncel siyasi olaylar — yeni kararlar, açıklamalar, atamalar, istifalar
- Türkiye siyaseti, dış politikası, ekonomisi — son haftalardaki gelişmeler
- Uluslararası çatışmalar, savaşlar, barış görüşmeleri (güncel)
- Ekonomik krizler, döviz/faiz hareketleri, makroekonomik gelişmeler
- Soruşturmalar, davalar, mahkeme kararları (kamu yararı olan)
- Yolsuzluk, skandallar, ifşalar
- Önemli sosyal/siyasi hareketler, protestolar
- Yorumcu/uzmanın belirli bir güncel olaya odaklanan analizleri

At (güncel gündem değil):
- Genel teorik veya tarihsel analizler — "finans kapital nedir", "Osmanlı nasıl çöktü" gibi belirli güncel olaya bağlı olmayan içerikler
- Felsefi/ideolojik söyleşiler, zamansız tartışmalar
- Sanatçı, oyuncu, şarkıcı, sporcu söyleşileri, magazin, biyografik içerik
- Kişisel hikayeler, anılar, hayata bakış, motivasyon sohbetleri
- Açıklayıcı/eğitim içeriği (güncel olaya bağlı değilse)
- Spor analizleri, eğlence, lifestyle, sağlık tavsiyeleri
- Reklam, sponsor içeriği, kanal duyuruları

ÖNEMLİ: Bir içerik birden fazla konu işliyorsa ve bunlardan EN AZ BİRİ güncel gündemse, o içeriği TUT.
Tereddütteysen tut — sadece bariz olarak gündem dışı olanları at.
""".strip()


def ts():
    """Returns current timestamp string for logging."""
    return datetime.now(ISTANBUL_TZ).strftime("%H:%M:%S")


def log(msg, indent=0):
    """Timestamped log line with flush."""
    prefix = "  " * indent
    print(f"[{ts()}] {prefix}{msg}", flush=True)


# ---------------------------------------------------------------------------
# Köşe yazısı çekme
# ---------------------------------------------------------------------------

def _norm(text):
    """Türkçe duyarlı normalizasyon: kıvrık kesme işaretini düzleştirir,
    İ/I harflerini doğru küçültür. Eşleşmeler bunun üzerinden yapılır."""
    return (text.replace("\u2019", "'").replace("\u2018", "'")
                .replace("İ", "i").replace("I", "ı")
                .lower().strip())


def _fetch_html(url, timeout=30, retries=3):
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=timeout)
            r.raise_for_status()
            if not r.encoding:
                r.encoding = "utf-8"
            return r.text
        except Exception as e:
            last = e
            if attempt < retries:
                log(f"🔁 İstek başarısız ({attempt}/{retries}): {str(e)[:120]}", indent=2)
                time.sleep(5 * attempt)
    raise last


def _extract_article_body(html):
    """(başlık, gövde) döndürür.

    CSS class adına bağlanmaz — sayfa yapısı değişse de çalışsın diye
    h1'den sonraki p/h2/h3 akışını sırayla toplar, dur işaretinde keser."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg",
                     "form", "nav", "header", "footer", "aside"]):
        tag.decompose()

    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else None

    started = h1 is None
    parts = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p"]):
        text = el.get_text(" ", strip=True)
        if el.name == "h1":
            started = True
            continue
        if not started or not text:
            continue
        low = _norm(text)
        if any(marker in low for marker in _STOP_MARKERS):
            break
        if low in _SKIP_LINES or _DATE_LINE_RE.match(text):
            continue
        if el.name in ("h2", "h3", "h4"):
            if "yazı içeriği" in low:   # sayfa içi içindekiler bloğu
                continue
            parts.append(f"\n## {text}\n")
        else:
            parts.append(text)

    body = re.sub(r"\n{3,}", "\n\n", "\n\n".join(parts)).strip()
    return title, body


def _discover_article_links(source):
    """[(url, 'YYYY-AA-GG'), ...] — liste sayfasındaki yazı linkleri."""
    html = _fetch_html(source["list_url"])
    found = {}
    for m in re.finditer(source["link_pattern"], html):
        url = urljoin(source["list_url"], m.group(1))
        found.setdefault(url, m.group(2))
    return sorted(found.items(), key=lambda kv: kv[1], reverse=True)


def get_latest_articles(target_date=None):
    """Belirtilen güne (varsayılan: DÜN, İstanbul takvimi) ait köşe yazılarını
    video dict'leriyle aynı şemada döndürür. 'transkript' anahtarı dolu gelir,
    böylece ana döngü ayrıca indirme yapmaz.

    Tarih, yazının URL'sinden okunur (/yazarlar/.../2026-08-04/slug); sitede
    yayın saati bulunmadığı için seçim gün bazlıdır. Aynı güne birden fazla
    yazı düşerse hepsi alınır."""
    if target_date is None:
        target_date = (datetime.now(ISTANBUL_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")

    log(f"📰 Köşe yazıları taranıyor (hedef gün: {target_date})")
    found = []

    for source in ARTICLE_SOURCES:
        try:
            links = _discover_article_links(source)
        except Exception as e:
            log(f"❌ HATA [{source['name']}] liste sayfası: {e}", indent=1)
            continue

        matching = [url for url, date in links if date == target_date]
        log(f"🔗 [{source['name']}] {len(links)} link tarandı, "
            f"{len(matching)} tanesi {target_date} tarihli", indent=1)

        for url in matching:
            try:
                html = _fetch_html(url)
            except Exception as e:
                log(f"❌ Yazı çekilemedi ({url}): {e}", indent=1)
                continue

            title, body = _extract_article_body(html)
            if len(body) < 400:
                log(f"⚠️ Gövde çıkarılamadı/çok kısa ({len(body)} karakter): {url}", indent=1)
                continue

            found.append({
                "name": source["name"],
                "title": title or url.rsplit("/", 1)[-1].replace("-", " "),
                "video_id": None,
                "url": url,
                "duration": "yazı",
                "kind": "article",
                "transkript": body,      # transkript gibi hazır gelir
            })
            log(f"✓ {title} — {len(body):,} karakter", indent=1)
            time.sleep(1)

    log(f"📰 Köşe yazısı taraması tamamlandı, {len(found)} yazı bulundu.")
    return found


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def is_short(video_id):
    res = requests.get(
        f"https://www.googleapis.com/youtube/v3/videos?part=contentDetails&id={video_id}&key={YOUTUBE_API_KEY}",
        timeout=30
    ).json()
    try:
        duration = res["items"][0]["contentDetails"]["duration"]
        seconds = isodate.parse_duration(duration).total_seconds()
        return seconds <= 180, f"{int(seconds // 60)} dk"
    except Exception:
        return False, "? dk"


def get_latest_video_list():
    log("📡 YouTube'dan son videolar çekiliyor...")
    found_videos = []
    yesterday_dt = datetime.now(timezone.utc) - timedelta(days=1)
    for name, handle in CHANNELS.items():
        c_res = None
        try:
            c_res = requests.get(
                f"https://www.googleapis.com/youtube/v3/channels?part=contentDetails&forHandle={handle}&key={YOUTUBE_API_KEY}",
                timeout=30
            ).json()
            uploads_id = c_res["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            res = requests.get(
                f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId={uploads_id}&maxResults=5&key={YOUTUBE_API_KEY}",
                timeout=30
            ).json()
            for item in res.get("items", []):
                pub_date = datetime.strptime(item["snippet"]["publishedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if pub_date > yesterday_dt:
                    video_id = item["snippet"]["resourceId"]["videoId"]
                    short, duration = is_short(video_id)
                    if not short:
                        found_videos.append({
                            "name": name,
                            "title": item["snippet"]["title"],
                            "video_id": video_id,
                            "duration": duration,
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                            "kind": "video",
                        })
        except Exception as e:
            log(f"❌ HATA [{name}]: {e}")
            if c_res is not None:
                log(f"   API yanıtı: {str(c_res)[:300]}", indent=1)
    log(f"📡 YouTube taraması tamamlandı, {len(found_videos)} uygun video bulundu.")
    return found_videos


def transkript_cek(video_id):
    for attempt in range(3):
        t0 = time.time()
        log(f"🎬 Apify transkript isteği (deneme {attempt+1}/3, video_id={video_id})...", indent=1)
        try:
            response = requests.post(
                "https://api.apify.com/v2/acts/starvibe~youtube-video-transcript/run-sync-get-dataset-items",
                params={"token": APIFY_API_KEY},
                json={"youtube_url": f"https://www.youtube.com/watch?v={video_id}"},
                timeout=120
            ).json()
            elapsed = time.time() - t0
            if response and len(response) > 0:
                all_segments = []
                for item in response:
                    all_segments.extend(item.get("transcript") or [])
                if all_segments:
                    transkript = " ".join([t.get("text", "") for t in all_segments])
                    log(f"✓ Transkript alındı ({len(transkript):,} karakter, {len(response)} item / {len(all_segments)} segment, {elapsed:.1f}s)", indent=1)
                    return transkript
                else:
                    log(f"⚠️ Transkript boş geldi ({len(response)} item ama hiçbirinde 'transcript' yok, {elapsed:.1f}s)", indent=1)
            else:
                log(f"⚠️ Apify boş yanıt verdi ({elapsed:.1f}s)", indent=1)
        except Exception as e:
            elapsed = time.time() - t0
            log(f"❌ Apify hatası ({elapsed:.1f}s): {e}", indent=1)
        if attempt < 2:
            log(f"⏳ 10 saniye bekleniyor, tekrar denenecek...", indent=1)
            time.sleep(10)
    log(f"❌ Transkript çekilemedi, 3 deneme de başarısız oldu", indent=1)
    return None


def _gemini_error_kind(e):
    """Classify a generate_content exception.
    'dead'      key blocked/invalid (403, bad key)        -> retire key on every model
    'daily'     free-tier daily quota spent (429 PerDay)  -> retire key on this model only
    'rate'      per-minute rate limit (429 PerMinute)     -> next key, retry later
    'transient' 5xx / overload / network                   -> next key
    'fatal'     anything else"""
    code = getattr(e, "code", None)
    status = str(getattr(e, "status", "") or "").upper()
    details = getattr(e, "details", None)
    blob = f"{status} {getattr(e, 'message', '')} {json.dumps(details, default=str) if details is not None else ''} {e}".lower()
    if code == 403 or "permission_denied" in blob:
        return "dead"
    if code == 400 and "api key" in blob:
        return "dead"
    if code == 429 or "resource_exhausted" in blob or "quota" in blob or "rate limit" in blob:
        if "perminute" in blob or "per minute" in blob:
            return "rate"
        return "daily"
    if (code is not None and 500 <= int(code) < 600) or any(s in blob for s in (
        "503", "502", "504", "unavailable", "overloaded", "high demand",
        "deadline", "timeout", "timed out", "cancelled", "canceled",
        "server disconnected", "remoteprotocolerror", "connection reset",
        "connection aborted", "connection closed", "broken pipe",
        "incomplete read", "empty response",
    )):
        return "transient"
    return "fatal"


def _live_keys(model):
    return [(i, k) for i, k in enumerate(GEMINI_API_KEYS, start=1)
            if i not in _dead_keys and (model, i) not in _quota_dead]


def call_gemini(prompt, label="Gemini", response_schema=None):
    """Shared helper: returns text or None.

    Runs on MODEL_CHAIN[0], trying each key in order. A key whose daily quota on that model is
    spent is retired for that model; once every key is spent on a model, the call moves to the
    next model in MODEL_CHAIN (same keys, fresh per-model quota). A 403/invalid key is retired
    on every model. If response_schema is provided, the model is forced to return JSON."""
    prompt_chars = len(prompt)
    log(f"🤖 [{label}] Gemini çağrısı (prompt: {prompt_chars:,} karakter)", indent=1)

    config_kwargs = {
        "thinking_config": types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.HIGH
        ),
    }
    if response_schema is not None:
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = response_schema

    for m_pos, model in enumerate(MODEL_CHAIN):
        keys = _live_keys(model)
        if not keys:
            if m_pos + 1 < len(MODEL_CHAIN):
                continue  # already spent — go straight to the next model
            break
        log(f"🧠 Model: {model} ({len(keys)} kullanılabilir key)", indent=2)

        for key_idx, key in keys:
            log(f"🔑 Key #{key_idx} deneniyor...", indent=2)
            client = genai.Client(api_key=key)
            for attempt in range(3):
                t0 = time.time()
                try:
                    log(f"📤 API isteği gönderildi (deneme {attempt+1}/3, thinking=HIGH)...", indent=2)
                    response = client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=types.GenerateContentConfig(**config_kwargs)
                    )
                    elapsed = time.time() - t0
                    if response.text:
                        out_chars = len(response.text)
                        usage_info = ""
                        try:
                            if hasattr(response, "usage_metadata") and response.usage_metadata:
                                u = response.usage_metadata
                                in_tok = getattr(u, "prompt_token_count", "?")
                                out_tok = getattr(u, "candidates_token_count", "?")
                                think_tok = getattr(u, "thoughts_token_count", "?")
                                usage_info = f", tokens: in={in_tok} out={out_tok} think={think_tok}"
                        except Exception:
                            pass
                        log(f"✅ [{label}] Yanıt alındı ({model}, {elapsed:.1f}s, {out_chars:,} karakter{usage_info})", indent=2)
                        return response.text.strip()
                    else:
                        log(f"⚠️ [{label}] Boş yanıt geldi ({elapsed:.1f}s)", indent=2)
                except Exception as e:
                    elapsed = time.time() - t0
                    err_str = str(e)
                    kind = _gemini_error_kind(e)
                    if kind == "dead":
                        _dead_keys[key_idx] = err_str[:120]
                        log(f"⛔ Key #{key_idx} kullanılamıyor (403/geçersiz), bu çalıştırmada bir daha denenmeyecek: {err_str[:160]}", indent=2)
                        break
                    if kind == "daily":
                        _quota_dead.add((model, key_idx))
                        log(f"⛔ Key #{key_idx} günlük kotası '{model}' için dolu ({elapsed:.1f}s), sıradaki key'e geçiliyor", indent=2)
                        break
                    if kind == "rate":
                        log(f"🐢 Key #{key_idx} dakikalık limit ({elapsed:.1f}s), sıradaki key'e geçiliyor", indent=2)
                        break
                    if kind == "transient":
                        log(f"🔁 Key #{key_idx} geçici hata ({elapsed:.1f}s): {err_str[:200]} — sıradaki key'e geçiliyor", indent=2)
                        break
                    wait = (attempt + 1) * 30
                    log(f"❌ API hatası ({elapsed:.1f}s, deneme {attempt+1}/3): {err_str[:200]}", indent=2)
                    log(f"⏳ {wait} saniye bekleniyor...", indent=2)
                    time.sleep(wait)

        if not _live_keys(model) and m_pos + 1 < len(MODEL_CHAIN):
            log(f"➡️ '{model}' kotası tüm key'lerde doldu, '{MODEL_CHAIN[m_pos + 1]}' modeline geçiliyor", indent=1)
            continue
        break  # keys remain on this model but all failed for other reasons — same outcome as before

    log(f"❌ [{label}] Tüm key'ler ve denemeler tükendi", indent=1)
    return None


def analyze_video(video, transkript):
    if video.get("kind") == "article":
        source_line = (f'{video["name"]} imzasıyla yayımlanan "{video["title"]}" '
                       f'başlıklı köşe yazısının tam metnidir.')
    else:
        source_line = (f'{video["name"]} kanalında yayınlanan "{video["title"]}" '
                       f'başlıklı videonun transkriptidir.')
    prompt = PROMPT_TEMPLATE.format(source_line=source_line, transkript=transkript)
    label = f"{video['name'][:20]} özet"
    result = call_gemini(prompt, label=label)
    if result:
        rapor_match = re.search(r"<rapor>(.*?)(?:</rapor>|$)", result, re.DOTALL)
        if rapor_match:
            return rapor_match.group(1).strip()
        return result
    return f"⚠️ [{video['name']}] \"{video['title']}\" — Tüm API keyleri tükendi."


def split_into_batches(items, target_size=3):
    """Split into balanced batches. e.g. n=7, target=3 → sizes [3,2,2]."""
    n = len(items)
    if n <= target_size:
        return [items]
    num_batches = math.ceil(n / target_size)
    base = n // num_batches
    extra = n % num_batches
    batches = []
    idx = 0
    for i in range(num_batches):
        size = base + (1 if i < extra else 0)
        batches.append(items[idx:idx + size])
        idx += size
    return batches


def consolidate_reports(reports_list):
    """Batch consolidation: group reports into ~3, consolidate each batch, then merge."""
    log(f"")

    if len(reports_list) <= 3:
        combined_text = "\n\n---\n\n".join(reports_list)
        log(f"🔄 KONSOLİDASYON başlatılıyor ({len(reports_list)} rapor, {len(combined_text):,} karakter)")
        prompt = CONSOLIDATION_PROMPT.format(combined_reports=combined_text)
        result = call_gemini(prompt, label="KONSOLİDASYON")
        if result:
            log(f"✅ Konsolidasyon tamamlandı ({len(result):,} karakter)")
            return result
        log(f"⚠️ Konsolidasyon başarısız, bireysel raporlar gönderiliyor")
        return combined_text

    batches = split_into_batches(reports_list, target_size=3)
    log(f"🔄 BATCH KONSOLİDASYON başlatılıyor ({len(reports_list)} rapor → {len(batches)} batch: {[len(b) for b in batches]})")

    batch_outputs = []
    for i, batch in enumerate(batches, 1):
        batch_text = "\n\n---\n\n".join(batch)
        log(f"")
        log(f"📦 Batch {i}/{len(batches)} işleniyor ({len(batch)} rapor, {len(batch_text):,} karakter)")
        prompt = CONSOLIDATION_PROMPT.format(combined_reports=batch_text)
        result = call_gemini(prompt, label=f"Batch {i}/{len(batches)}")
        if result:
            log(f"✅ Batch {i} tamamlandı ({len(result):,} karakter)")
            batch_outputs.append(result)
        else:
            log(f"⚠️ Batch {i} başarısız, ham raporlar fallback olarak kullanılacak")
            batch_outputs.append(batch_text)

    log(f"")
    merge_input = "\n\n=== BATCH AYIRICI ===\n\n".join(
        f"[BATCH {i+1}]\n\n{out}" for i, out in enumerate(batch_outputs)
    )
    log(f"🔀 FİNAL BİRLEŞTİRME başlatılıyor ({len(batch_outputs)} batch raporu, {len(merge_input):,} karakter)")
    prompt = FINAL_MERGE_PROMPT.format(batch_reports=merge_input)
    final = call_gemini(prompt, label="FİNAL BİRLEŞTİRME")
    if final:
        log(f"✅ Final birleştirme tamamlandı ({len(final):,} karakter)")
        return final
    log(f"⚠️ Final birleştirme başarısız, batch çıktıları yan yana gönderiliyor")
    return "\n\n---\n\n".join(batch_outputs)


def filter_video_reports(items):
    """Drop reports that don't match INTERESTS. items: list of {video, report}.
    Returns filtered list. On filter failure, returns the original list (don't lose work)."""
    if not items:
        return items

    payload = "\n\n=== İÇERİK AYIRICI ===\n\n".join(
        f"[{i}] KANAL: {it['video']['name']}\n"
        f"BAŞLIK: {it['video']['title']}\n\n"
        f"RAPOR:\n{it['report']}"
        for i, it in enumerate(items)
    )

    schema = {
        "type": "object",
        "properties": {
            "drop_indices": {
                "type": "array",
                "items": {"type": "integer"},
            },
        },
        "required": ["drop_indices"],
    }

    prompt = (
        f"{payload}\n\n"
        f"{INTERESTS}\n\n"
        "Yukarıdaki içerik raporlarını yukarıdaki kriterlere göre değerlendir. "
        "AT kategorisine giren içeriklerin indekslerini döndür. "
        "Tereddütteysen indekse koyma — sadece bariz gündem dışı olanları at."
    )

    log(f"")
    log(f"🔍 İLGİ FİLTRESİ başlatılıyor ({len(items)} içerik değerlendiriliyor)")
    try:
        result = call_gemini(prompt, label="FİLTRE", response_schema=schema)
        if not result:
            log(f"⚠️ Filtre çağrısı sonuçsuz, tüm raporlar tutuluyor")
            return items
        data = json.loads(result)
        drop = {i for i in data.get("drop_indices", []) if 0 <= i < len(items)}
    except Exception as e:
        log(f"⚠️ Filtre hatası ({e}); tüm raporlar tutuluyor")
        return items

    kept = [it for i, it in enumerate(items) if i not in drop]
    log(f"✅ Filtre: {len(kept)}/{len(items)} tutuldu, {len(drop)} atıldı")
    if drop:
        for i in sorted(drop):
            v = items[i]['video']
            log(f"⊘ ATILDI [{v['name']}] {v['title'][:60]}", indent=1)
    return kept


def send_to_discord(report):
    """Send the report to Discord as chunked text messages. Ping fires on the first chunk only."""
    log(f"📤 Discord'a gönderiliyor (toplam {len(report):,} karakter)...")
    chunks_sent = 0
    first = True
    while report:
        if len(report) <= 1900:
            chunk = report
            report = ""
        else:
            split_at = report.rfind("\n", 0, 1900)
            if split_at == -1:
                split_at = report.rfind(" ", 0, 1900)
            if split_at == -1:
                split_at = 1900
            chunk = report[:split_at]
            report = report[split_at:].lstrip()
        payload = {"content": chunk}
        if first:
            payload["content"] = f"<@331085172264927235>\n{chunk}"
            payload["allowed_mentions"] = {"parse": [], "users": ["331085172264927235"]}
            first = False
        attempts = 0
        while True:
            attempts += 1
            try:
                r = requests.post(DISCORD_URL, json=payload, timeout=30)
                if r.status_code == 429 and attempts < 3:
                    try:
                        retry = r.json().get("retry_after", 5)
                    except Exception:
                        retry = 5
                    log(f"⚠️ Discord rate-limit, {retry}s bekleniyor (deneme {attempts}/3)", indent=1)
                    time.sleep(float(retry) + 0.5)
                    continue
                if r.status_code >= 400:
                    log(f"⚠️ Discord {r.status_code}: {r.text[:200]}", indent=1)
                chunks_sent += 1
                break
            except Exception as e:
                log(f"❌ Discord gönderim hatası: {e}", indent=1)
                break
        time.sleep(0.5)
    log(f"✅ Discord gönderimi tamamlandı ({chunks_sent} parça)")




def _safe_filename(name, max_len=100):
    """Strip characters Discord/Windows reject and cap the length."""
    name = re.sub(r"[\\/]", "-", name)
    name = re.sub(r'[:*?"<>|\n\r\t]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:max_len].rstrip(" .") or "Rapor"


# DejaVu tüm Türkçe karakterleri içerir; emoji içermez. Emoji ve benzeri
# desteklenmeyen karakterler fontun kendi glif tablosuna bakılarak atılır,
# aksi halde PDF'te siyah kutu olarak basılır.
_FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
]

_PDF_FONT = None          # (regular_name, bold_name)
_SUPPORTED_CHARS = None   # fontun desteklediği kod noktaları


def _register_pdf_fonts():
    global _PDF_FONT, _SUPPORTED_CHARS
    if _PDF_FONT:
        return _PDF_FONT
    for regular, bold in _FONT_CANDIDATES:
        if os.path.exists(regular) and os.path.exists(bold):
            pdfmetrics.registerFont(RLTTFont("ReportFont", regular))
            pdfmetrics.registerFont(RLTTFont("ReportFont-Bold", bold))
            pdfmetrics.registerFontFamily("ReportFont", normal="ReportFont",
                                          bold="ReportFont-Bold",
                                          italic="ReportFont",
                                          boldItalic="ReportFont-Bold")
            face = pdfmetrics.getFont("ReportFont").face
            _SUPPORTED_CHARS = set(getattr(face, "charToGlyph", {}) or {})
            _PDF_FONT = ("ReportFont", "ReportFont-Bold")
            return _PDF_FONT
    raise RuntimeError(
        "PDF için Unicode font bulunamadı. Ubuntu'da: "
        "sudo apt-get install -y fonts-dejavu-core")


# Görünmez karakterler: fontta glif olarak BULUNURLAR, dolayısıyla glif
# filtresine takılmazlar; ayrıca strip() de silmez. Emoji'nin varyasyon
# seçicisi (FE0F) geride kalıp satır başı eşleşmelerini bozar → ayrıca sil.
_ZERO_WIDTH = dict.fromkeys(
    map(ord, "\ufe0f\ufe0e\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)


def _sanitize(text):
    """Emoji ve fontta karşılığı olmayan karakterleri at, görünmezleri temizle."""
    text = text.translate(_ZERO_WIDTH)
    if not _SUPPORTED_CHARS:
        return text
    return "".join(ch for ch in text
                   if ch in ("\n", "\t") or ord(ch) in _SUPPORTED_CHARS)


def _inline_md(text):
    """Markdown satır içi biçimlendirmeyi ReportLab etiketlerine çevirir.
    XML kaçışı ÖNCE yapılır, yoksa metindeki < & işaretleri PDF'i bozar."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
    return text


_BANNER_RE = re.compile(r"^[A-ZÇĞİÖŞÜ0-9\s\-–—:()]{6,}$")


def build_pdf_report(report_text, doc_title="Gündem Raporu"):
    """Konsolide raporu tek bir PDF'e basar, bytes döndürür."""
    regular, bold = _register_pdf_fonts()

    base = getSampleStyleSheet()
    body = ParagraphStyle("Body", parent=base["Normal"], fontName=regular,
                          fontSize=9.5, leading=14, alignment=TA_JUSTIFY,
                          spaceAfter=6)
    bullet = ParagraphStyle("Bullet", parent=body, leftIndent=12,
                            bulletIndent=2, spaceAfter=4,
                            bulletFontName=regular, bulletFontSize=9.5)
    h1 = ParagraphStyle("H1", parent=base["Heading1"], fontName=bold,
                        fontSize=15, leading=19, spaceBefore=14, spaceAfter=8,
                        textColor=colors.HexColor("#1a1a1a"))
    h2 = ParagraphStyle("H2", parent=base["Heading2"], fontName=bold,
                        fontSize=12, leading=16, spaceBefore=12, spaceAfter=6,
                        textColor=colors.HexColor("#222222"))
    h3 = ParagraphStyle("H3", parent=base["Heading3"], fontName=bold,
                        fontSize=10.5, leading=14, spaceBefore=9, spaceAfter=4)
    banner = ParagraphStyle("Banner", parent=h1, fontSize=13, leading=17,
                            spaceBefore=18, spaceAfter=4,
                            textColor=colors.HexColor("#0b5394"))
    title_style = ParagraphStyle("DocTitle", parent=h1, fontSize=18, leading=22,
                                 spaceBefore=0, spaceAfter=2)

    story = [Paragraph(_inline_md(_sanitize(doc_title)), title_style),
             HRFlowable(width="100%", thickness=1,
                        color=colors.HexColor("#999999"), spaceAfter=10)]

    for raw in report_text.split("\n"):
        line = _sanitize(raw).strip()
        if not line:
            story.append(Spacer(1, 4))
            continue
        if re.fullmatch(r"-{3,}", line):
            story.append(HRFlowable(width="100%", thickness=0.6,
                                    color=colors.HexColor("#cccccc"),
                                    spaceBefore=8, spaceAfter=8))
            continue
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            style = {1: h1, 2: h2}.get(len(m.group(1)), h3)
            story.append(Paragraph(_inline_md(m.group(2)), style))
            continue
        m = re.match(r"^[-*•]\s+(.+)$", line)
        if m:
            story.append(Paragraph(_inline_md(m.group(1)), bullet, bulletText="•"))
            continue
        plain = re.sub(r"[*`]", "", line)
        if _BANNER_RE.fullmatch(plain) and len(plain) >= 6:
            story.append(Paragraph(_inline_md(plain.strip()), banner))
            story.append(HRFlowable(width="100%", thickness=0.8,
                                    color=colors.HexColor("#0b5394"),
                                    spaceAfter=6))
            continue
        story.append(Paragraph(_inline_md(line), body))

    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(regular, 8)
        canvas.setFillColor(colors.HexColor("#888888"))
        canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, str(canvas.getPageNumber()))
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm,
                            title=doc_title, author="Gündem Botu")
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()




def upload_report_files_to_discord(parts, message):
    """Upload (filename, text) parts as attachments. Discord allows max 10 files per
    message, so parts go out in batches; the message text rides on the first batch only."""
    total_chars = sum(len(t) for _, t in parts)
    log(f"📎 Discord'a {len(parts)} dosya yükleniyor ({total_chars:,} karakter)...")
    for batch_no, start in enumerate(range(0, len(parts), 10), 1):
        batch = parts[start:start + 10]
        content = message if start == 0 else f"{message} (devam {batch_no})"
        _upload_file_batch(batch, content)


def _upload_file_batch(batch, message):
    payload = {"content": message}
    files = {"payload_json": (None, json.dumps(payload), "application/json")}
    for i, (fname, content) in enumerate(batch):
        data = content if isinstance(content, (bytes, bytearray)) else content.encode("utf-8")
        mime = "application/pdf" if fname.lower().endswith(".pdf") else "text/markdown"
        files[f"files[{i}]"] = (fname, data, mime)
    for attempt in range(1, 4):
        try:
            r = requests.post(DISCORD_URL, files=files, timeout=60)
            if r.status_code == 429 and attempt < 3:
                try:
                    retry = r.json().get("retry_after", 5)
                except Exception:
                    retry = 5
                log(f"⚠️ Discord rate-limit, {retry}s bekleniyor (deneme {attempt}/3)", indent=1)
                time.sleep(float(retry) + 0.5)
                continue
            if r.status_code >= 400:
                log(f"⚠️ Discord {r.status_code}: {r.text[:200]}", indent=1)
            else:
                log(f"✅ Discord dosya yükleme tamamlandı")
            return
        except Exception as e:
            log(f"❌ Discord dosya yükleme hatası: {e}", indent=1)
            return


if __name__ == "__main__":
    run_start = time.time()
    log(f"🚀 BAŞLATILIYOR")

    videos = get_latest_video_list()
    videos += get_latest_articles()

    if not videos:
        log("Son 24 saatte yeni video/yazı bulunamadı.")
    else:
        log(f"📋 Bulunan içerikler ({len(videos)} adet):")
        for v in videos:
            log(f"  - [{v['name']}] {v['title']} ({v['duration']})")

        analyzed = []  # her eleman: {"video": video, "report": report}

        for i, video in enumerate(videos):
            video_start = time.time()
            log(f"")
            log(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            log(f"[{i+1}/{len(videos)}] İşleniyor: [{video['name']}] {video['title']}")
            log(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

            # Köşe yazılarında metin zaten hazır; sadece videolar için indir.
            transkript = video.get("transkript") or transkript_cek(video["video_id"])
            if not transkript:
                log(f"⚠️ Transkript alınamadı, içerik atlanıyor", indent=1)
                continue

            log(f"📝 İlk 300 karakter: {transkript[:300]}...", indent=1)
            report = analyze_video(video, transkript)
            video["transkript"] = transkript
            analyzed.append({"video": video, "report": report})
            video_elapsed = time.time() - video_start
            log(f"✅ İçerik tamamlandı ({video_elapsed:.1f}s, rapor: {len(report):,} karakter)", indent=1)
            time.sleep(2)

        if not analyzed:
            log("❌ Hiçbir içerik işlenemedi.")
        else:
            analyzed = filter_video_reports(analyzed)
            if not analyzed:
                log("❌ İlgi filtresi tüm içerikleri eledi, rapor üretilmiyor.")

        if analyzed:
            all_reports = [
                f"**{idx}.** 📺 **[{it['video']['name']}]** — {it['video']['title']} `({it['video']['duration']})`\n\n{it['report']}"
                for idx, it in enumerate(analyzed, 1)
            ]
            processed_videos = [it["video"] for it in analyzed]

            current_date = datetime.now(ISTANBUL_TZ).strftime("%d.%m.%Y")

            final_report_body = consolidate_reports(all_reports)

            videos_list = "\n".join(
                f"- **[{v['name']}]** {v['title']} `({v['duration']})`"
                for v in processed_videos
            )
            full_report = (
                f"📅 **{current_date}**\n\n"
                f"📺 **İncelenen İçerikler ({len(processed_videos)} adet):**\n"
                f"{videos_list}\n\n"
                f"---\n\n"
                f"{final_report_body}"
            )

            send_to_discord(full_report)

            header_msg = f"**Youtube {datetime.now(ISTANBUL_TZ).strftime('%d %m %Y')}**"
            pdf_name = _safe_filename(f"Gundem Raporu {current_date}") + ".pdf"
            try:
                pdf_bytes = build_pdf_report(full_report, f"Gündem Raporu — {current_date}")
                log(f"📄 PDF oluşturuldu: {pdf_name} ({len(pdf_bytes):,} bayt)")
                upload_report_files_to_discord([(pdf_name, pdf_bytes)], header_msg)
            except Exception as e:
                log(f"⚠️ PDF oluşturulamadı ({e}); rapor .md olarak yükleniyor")
                upload_report_files_to_discord([("Rapor.md", full_report)], header_msg)

            total_elapsed = time.time() - run_start
            mins = int(total_elapsed // 60)
            secs = int(total_elapsed % 60)
            log(f"")
            log(f"🏁 İŞLEM TAMAMLANDI")
            log(f"   • {len(all_reports)} içerik işlendi")
            log(f"   • Toplam süre: {mins} dk {secs} sn")
            log(f"   • Konsolide rapor Discord'a gönderildi (tek PDF)")

            transcript_filename = f"transkriptler_{datetime.now(ISTANBUL_TZ).strftime('%Y%m%d_%H%M%S')}.txt"
            total_chars = 0
            with open(transcript_filename, "w", encoding="utf-8") as f:
                for i, v in enumerate(processed_videos, 1):
                    text = v.get("transkript", "(transkript yok)")
                    total_chars += len(text)
                    f.write("=" * 70 + "\n")
                    f.write(f"[{i}/{len(processed_videos)}] {v['name']} — {v['title']} ({v['duration']})\n")
                    f.write(f"{v.get('url', '')}\n")
                    f.write("=" * 70 + "\n")
                    f.write("\n")
                    f.write(text)
                    f.write("\n\n")
            log(f"📝 Transkriptler dosyaya yazıldı: {transcript_filename} ({total_chars:,} karakter)")
