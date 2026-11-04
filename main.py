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


# Rapor, özet değil konu konu ayrıntılı bir dökümdür; asıl ölçüt okunabilirlik.
# Kısaltılmış metinde cümleler arasındaki bağ kopar, okuyucu anlamaz: bu yüzden
# bilgi atılmaz (yalnızca sohbet, duyuru ve tekrar atılır), her konu bağlamıyla açılır,
# içerik yorumcunun anlattığı gibi tek bir akan anlatıdır (olgu ve yorum ayrılmaz,
# etiketlenmez). Cümleler modelin, bilgiler içeriğin: içerikte olmayan
# hiçbir bilgi, bağlam, neden-sonuç bağı veya değerlendirme eklenmez.
PROMPT_TEMPLATE = """<transkript>
{transkript}
</transkript>

<görev>
Yukarıdaki metin, {source_line}

<amaç>
Bu içeriği izlemeyen veya okumayan biri, raporu okuyunca içerikte işlenen her konuyu, her konudaki bilgileri, iddiaları ve yorumları eksiksiz öğrensin ve ilk okuyuşta anlasın. Rapor bir özet değil, konu konu düzenlenmiş ayrıntılı bir dökümdür. Uzun bir metni kısaltırken cümleler arasındaki bağ kopar ve okuyucu ne anlatıldığını anlayamaz; bu raporun asıl ölçütü okunabilirliktir.
Cümleler senin, bilgiler içeriğin. Rapordaki her bilgi içerikte geçer; içerikte olmayan hiçbir bilgi, bağlam, görev, sebep, sonuç veya değerlendirme eklenmez. Okunabilirlik için yalnızca cümle kurarsın, bilgi eklemezsin.
</amaç>

<neyi_tut>
İçerikte geçen her haber konusunu ve her konudaki bütün bilgiyi tut:
- olaylar, kararlar, açıklamalar ve bunların kimden, ne zaman, hangi kaynaktan (ajans, gazete, televizyon, sosyal medya) geldiği;
- isimler, kişilerin metinde geçen görevleri, sayılar, tarihler, yerler, doğrudan alıntılar;
- yorumcunun kendi bilgileri ve iddiaları ("kesin bilgi", kulis), bunların gerekçesi ve yorumcunun bu konuda daha önce ne söylediği;
- yorumcunun yorumları, değerlendirmeleri ve sorduğu sorular.
Bir bilgiyi önemsiz bulduğun için atma. Tek ölçüt: haber, iddia veya yorum taşıyor mu?
</neyi_tut>

<neyi_at>
Yalnızca haber, iddia veya yorum taşımayan kısımları at:
- açılış ve kapanış sohbeti, selamlaşma, şakalar, gündelik muhabbet;
- kanal ve program duyuruları, sonraki programın tanıtımı, abonelik, bağış ve destek çağrıları (Patreon, Kreosus, YouTube katıl vb.), sponsor ve reklam;
- yayının kendi akışına dair konuşmalar ("kesiti verelim", "altyazıya bakın", "ekrana getirelim", "buna sonra geleceğiz");
- aynı bilginin tekrarı: yorumcu bir şeyi birkaç kez söylüyorsa bir kez yaz.
</neyi_at>

<okunabilirlik>
1. Her konu kendi bağlamıyla açılır. Okuyucu içeriği izlemedi: konunun ilk cümlesi ne olduğunu, kimin yaptığını ve ne zaman olduğunu söylesin. Konunun ortasından başlama.
2. Bir kişi veya kurum ilk geçtiğinde metinde söylenen görevi veya bağlamıyla tanıtılır: "Akın Gürlek'in danışmanı Furkan Torlak", "PKK yöneticisi Murat Karayılan". Görev metinde geçmiyorsa uydurma, yalnızca ismi yaz.
3. İçeriği yorumcunun anlattığı gibi tek bir akan anlatı olarak yaz. Yorumcunun aktardığı olayları, kendi bilgilerini, iddialarını ve yorumlarını birbirinden ayırma: ayrı paragrafa, ayrı satıra veya etikete ("Yorum:") koyma, her cümlenin başına "Atay'a göre" ekleme. Rapor zaten o yorumcunun içeriğinin raporudur; okuyucu anlatının yorumcuya ait olduğunu bilir.
   - Yorumcunun adı yalnızca kendisinden söz ettiği yerde geçer: daha önce ne söylediği, başına ne geldiği, ne sorduğu ("Özdil o dönemde bu bilgilerin sızdırıldığını anlatmıştı").
   - Başka birinin sözü, açıklaması veya iddiası o kişinin adıyla verilir: "Kılıçdaroğlu KRT'de Halk TV'nin 24 saat yalan haber yaptığını söyledi."
   - İçerikte bir şey iddia olarak geçiyorsa raporda da iddia olarak kalır.
4. Cümleleri birbirine bağla. Kopuk bilgi dizisi yazma: her cümle bir öncekinden nasıl çıktığını göstersin ("çünkü", "bu yüzden", "ancak", "buna karşılık", "bunun üzerine", "aynı günlerde"). Bağlacı yalnızca içerikte o ilişki kuruluyorsa kullan: "çünkü" yazdığın yerde içerikte de bir sebep söylenmiş olmalı. Bir cümlede en fazla iki-üç bilgi olsun; noktalı virgülle bilgi istifleme.
5. Belirsiz gönderme yapma. "Bu", "o", "bunlar", "konu" kelimelerinin neyi kastettiği açık değilse ismi tekrarla.
6. Uzun konuyu böl. Bir konunun birkaç yönü varsa kısa paragraflara veya kalın etiketli maddelere ayır ("**Kariyeri:** ...", "**Hakkındaki iddialar:** ..."). Bir paragraf en fazla 5-6 cümle olsun.
7. Benzetme yerine somut bilgi. Yorumcunun benzetmelerini ve süslü sözlerini ("deprem", "kış geliyor", "kolu kanadı kırıldı", "fitili ateşledi") kopyalama. Benzetmenin arkasındaki somut iddia içerikte söyleniyorsa onu yaz; söylenmiyorsa benzetmeyi at, ondan kendin anlam çıkarma. Bir kişinin sözü doğrudan alıntı olarak veriliyorsa tırnak içinde kalabilir.
8. Aktarım fiilleri sade olsun: "dedi", "diyor", "söylüyor", "iddia ediyor", "soruyor", "-e göre". "Belirtmektedir", "ifade etmektedir", "vurgulamaktadır", "öne sürmektedir" gibi resmi kalıpları kullanma.
</okunabilirlik>

<kısıtlar>
- Metinde olmayan bilgi, bağlam, sebep veya görev ekleme. Kendi yorumunu ekleme.
- Yorumcunun argümanını çarpıtma, yumuşatma veya sertleştirme.
- Sayı, tarih, isim ve alıntılar birebir doğru olsun. Transkriptte bozuk yazılmış bir isim metnin başka bir yerinde doğru geçiyorsa doğru yazımı kullan.
- Dil: metnin dili (Türkçe ise Türkçe).
- Konuları içerikteki sırayla yaz.
</kısıtlar>

<örnek>
<örnek_transkript>
Şimdi bakın, dün gece Merkez Bankası bir duyuru yaptı. Faizi 250 baz puan birden indirdiler. 50'den 47,5'e. Bu beklenmedik bir hamleydi çünkü piyasa beklentisi sadece 100 baz puan civarındaydı. Açıklamada Karahan "enflasyon eğilimi olumlu" dedi ama dolar bir gecede 35,80'den 36,20'ye fırladı. Yani piyasa karara güvenmedi. Erdoğan ise sabah konuşmasında "ekonomide normalleşme dönemi" diyerek bu indirimi savundu. Aslında bu 2024'ten beri uygulanan yüksek faiz politikasının bir dönüm noktası. Hatırlarsanız 2023'te Şimşek geldiğinde faizi 8,5'tan 50'ye çıkarmıştık. Neyse, programın sonunda Patreon destekçilerimize teşekkür edelim.
</örnek_transkript>

<yanlış_rapor>
### Faiz İndirimi
Merkez Bankası 250 baz puan indirdi, beklenti 100'dü; dolar 36,20, Karahan "olumlu", Erdoğan "normalleşme"; 2024'ten beri süren politikanın dönüm noktası, Şimşek 2023'te 8,5'ten 50'ye.
</yanlış_rapor>
Neden yanlış: bilgiler tek cümleye istiflenmiş, aralarındaki bağ kopmuş; hangi sözün kime ait olduğu anlaşılmıyor.

<yanlış_rapor>
### Merkez Bankası Başkanı Karahan'dan Sürpriz İndirim
Merkez Bankası Başkanı Karahan'ın yönetimindeki banka faizi 250 baz puan indirdi. Bu karar enflasyonu yeniden yükseltebilir.
</yanlış_rapor>
Neden yanlış: içerikte Karahan'ın görevi geçmiyor ve kararın enflasyona etkisinden hiç söz edilmiyor; ikisi de eklenmiş bilgi.

<yanlış_rapor>
### Merkez Bankası Faizi Beklentinin Üzerinde İndirdi
Merkez Bankası politika faizini 250 baz puan indirerek yüzde 47,5'e çekti. Dolar 35,80 liradan 36,20 liraya çıktı.
**Yorum:** Yorumcuya göre piyasa karara güvenmedi. Yorumcuya göre bu indirim yüksek faiz politikasında bir dönüm noktası.
</yanlış_rapor>
Neden yanlış: yorumcunun söyledikleri olaydan koparılıp etiketlenmiş ve her cümle "yorumcuya göre" diye başlıyor; anlatı bölünüyor ve okumak zorlaşıyor.

<doğru_rapor>
### Merkez Bankası Faizi Beklentinin Üzerinde İndirdi
Merkez Bankası dün gece politika faizini 250 baz puan indirerek yüzde 50'den yüzde 47,5'e çekti. Piyasa yalnızca 100 baz puan civarında bir indirim beklediği için karar sürpriz oldu. Karahan açıklamada "enflasyon eğilimi olumlu" dedi, ancak piyasa karara güvenmedi ve dolar bir gecede 35,80 liradan 36,20 liraya çıktı. Erdoğan ise sabah konuşmasında indirimi "ekonomide normalleşme dönemi" diyerek savundu. Bu indirim, 2024'ten beri uygulanan yüksek faiz politikasında bir dönüm noktası. 2023'te Şimşek geldiğinde faiz yüzde 8,5'ten yüzde 50'ye çıkarılmıştı.
</doğru_rapor>
Patreon teşekkürü haber taşımadığı için rapora girmedi.
</örnek>

<çıktı>
Her konu için "### Başlık" ve altında paragraflar. Başlık konuyu somut olarak söylesin: kim, ne yaptı. Raporu <rapor> ... </rapor> arasına yaz; öncesine hiçbir giriş cümlesi ekleme.
</çıktı>
</görev>"""

# Konsolidasyon ve final birleştirmenin ortak kuralları. Girdi raporları ayrıntılıdır;
# birleştirme bilgi atmaz, yalnızca kanallar arasındaki tekrarı tek yere toplar.
_MERGE_RULES = """<amaç>
Raporu okuyan biri, bütün kanallarda işlenen her konuyu, her konudaki bilgileri ve hangi kanalın ne söylediğini eksiksiz öğrensin ve ilk okuyuşta anlasın. Girdi raporları ayrıntılıdır: birleştirirken bilgi atma, yalnızca tekrarları birleştir.
</amaç>

<kurallar>
1. DIŞ BİLGİ YOK. Cümleler senin, bilgiler girdinin. Rapor yalnızca girdi raporlarındaki bilgileri kullanır. Kendi bilginden veya dış kaynaklardan hiçbir şey (kişinin görevi, olayın tarihi, arka plan, tarihsel referans) ekleme; kendi çıkarımını, değerlendirmeni veya girdide kurulmamış bir neden-sonuç bağını da ekleme. Girdide olmayan bir şey raporda yoktur.

2. BİLGİ ATMA, TEKRARI BİRLEŞTİR. Girdideki her olgu, isim, sayı, tarih, alıntı, iddia ve yorum raporda bir kez yer alır. Hiçbir bilgiyi önemsiz diye atma. Aynı bilgiyi iki kez yazma; aynı olayı önce genel sonra ayrıntılı diye iki kez anlatma. Sayı, isim ve alıntılar girdideki haliyle birebir doğru kalır.

3. ORTAK BİLGİLER ORTAK, KATKILAR ÖZGÜN. Birden fazla kanalın paylaştığı bilgiler (olayın kendisi, ortak aktörler, ortak gerçekler, ortak alıntılar) "Ortak Bilgiler" bölümünde bir kez yazılır. Her kanalın katkısında o kanala özgü olan her şey yer alır: başka kanalda geçmeyen bilgi, isim, sayı, alıntı, iddia ve yorum. Kanal katkısı kısa olmak zorunda değildir; o kanala özgü bilgi ne kadarsa o kadar yazılır.

4. OKUNABİLİRLİK. Kısaltılmış metinde cümleler arasındaki bağ kopar ve okuyucu anlamaz. Bu yüzden:
   - Her konu kendi bağlamıyla açılır: ilk cümle ne olduğunu, kimin yaptığını ve ne zaman olduğunu söyler.
   - Bir kişi veya kurum ilk geçtiğinde girdide geçen görevi veya bağlamıyla tanıtılır.
   - Cümleler birbirine bağlanır ("çünkü", "bu yüzden", "ancak", "buna karşılık", "bunun üzerine"). Bağlaç yalnızca girdide o ilişki kuruluyorsa kullanılır. Bir cümlede en fazla iki-üç bilgi olur; noktalı virgülle bilgi istiflenmez.
   - "Bu", "o", "bunlar" neyi kastettiği açık değilse ismi tekrarla.
   - Uzun bir konu kısa paragraflara veya kalın etiketli maddelere bölünür. Bir paragraf en fazla 5-6 cümle olur.

5. AYRIŞTIRMA DEĞİL, AKAN ANLATI.
   - Ortak Bilgiler'de bilgiyi hangi kanalların verdiği anlatıdan anlaşılır ("Atay ve Soykan ... anlattı", "Yetkin ve Altaylı ... aktardı"). Her cümleye ayrı kaynak listesi sıkıştırma.
   - Kanal Katkıları'nda ve Kanallara Özel Haberler'de kanal adı başlıkta yazdığı için içerik, o kanalın anlattığı gibi tek bir akan anlatı olarak yazılır. Kanalın aktardığı olaylar, kendi bilgileri, iddiaları ve yorumları birbirinden ayrılmaz: ayrı satıra veya etikete ("Yorum:") konmaz, her cümleye "Atay'a göre" eklenmez. Yorumcunun adı yalnızca kendisinden söz ettiği yerde geçer (daha önce ne söylediği, başına ne geldiği, ne sorduğu).
   - Kanalın aktardığı başka birinin sözü o kişinin adıyla verilir ("Karayılan, '...' diyor"). Girdide iddia olarak geçen bir şey raporda da iddia olarak kalır.
   - Aktarım fiilleri sade olur: "dedi", "diyor", "iddia ediyor", "soruyor", "-e göre". "Belirtmektedir", "ifade etmektedir", "vurgulamaktadır", "öne sürmektedir" kullanılmaz.

6. BENZETME YERİNE SOMUT BİLGİ. Yorumcuların benzetmelerini ve süslü sözlerini ("deprem", "siyasi rüzgar", "kasırga", "damga vurdu", "fitili ateşledi") kopyalama. Arkasındaki somut iddia girdide söyleniyorsa onu yaz ve cümleler arasındaki neden-sonuç bağını koru; söylenmiyorsa benzetmeyi at, ondan kendin anlam çıkarma. "Büyük", "kritik", "tarihi", "çarpıcı" gibi sıfatları, onları doğuran somut bilgi (sayı, oran, karşılaştırma) cümlede yoksa kullanma.
   Yanlış: "Özel'in mitinglerde yakaladığı büyük ivme ve meydanlarda oluşan müthiş dalga, yeni bir siyasi rüzgar yarattı."
   Doğru: "Özel mitinglerde ivme kazandı; bu ivme Kılıçdaroğlu'nun tutumundan bağımsız."

7. KÜMELEME. Aynı somut olayı (aynı karar, aynı açıklama, aynı eylem, aynı süreç) birden fazla kanal işlemişse konu "📌 ORTAK GÜNDEM" bölümüne, yalnızca bir kanal işlemişse "🎙️ KANALLARA ÖZEL HABERLER" bölümüne girer. Aynı kişi veya kurum adının iki haberde geçmesi birleştirmek için yetmez: "Özel'in parti kurma stratejisi" ile "Özel'in eşit yurttaşlık açıklaması" ayrı konulardır. Şüphedeysen ayrı tut. Birbiriyle ilgisiz konuları soyut bir başlık altında birleştirme. ORTAK GÜNDEM'deki konuları en çok kanalın işlediği konu en üstte olacak şekilde azalan sırayla diz.

8. BAŞLIK. ORTAK GÜNDEM başlıkları spesifik olur ve haberin asıl gerilimini taşır: somut aktör + somut eylem + hikâyedeki çarpıcı unsur (beklenmedik sonuç, çelişki, rakam, cepheleşme). Aktif fiil kullan, en önemli bilgiyi başa koy. Başlığın işaret ettiği şey raporda açıkça cevaplanır. Bilgiyi saklayan kalıplar ("Bakın ne oldu", "Şok karar"), ünlem, BÜYÜK HARF vurgusu ve cevapsız soru başlığı yasaktır.
   Fazla düz: "Merkez Bankası Faiz Kararı"
   Clickbait: "Merkez Bankası'ndan ŞOK Karar — Piyasalar Alt Üst!"
   Doğru: "Beklenti 100 Baz Puandı: Merkez Bankası 250 İndirdi, Dolar 36'yı Aştı"
</kurallar>

<şablon>
Aşağıdaki şablonu birebir kullan:

📌 ORTAK GÜNDEM

### [Spesifik Konu Başlığı]
**Ortak Bilgiler:** Olay ve birden fazla kanalın paylaştığı bütün bilgiler, bir kez. Gerekirse birden çok kısa paragraf.
**Kanal Katkıları:**
- **[Kanal Adı]:** Bu kanala özgü bütün bilgi, iddia ve yorum. Ortak Bilgiler'de yazılanı tekrarlama. Gerekirse birden çok cümle veya kısa paragraf; kanalın anlattığı gibi akan bir anlatı. Kanalın söyledikleri tamamen Ortak Bilgiler'de varsa "Ek bilgi yok." yaz.
- **[Diğer Kanal Adı]:** ...

---

🎙️ KANALLARA ÖZEL HABERLER

### [Kanal Adı]

**[Konu 1 Başlığı]**
Konunun bütün bilgisi, bağlamıyla açılan kısa paragraflar halinde.

**[Konu 2 Başlığı]**
Aynı şekilde.

### [Diğer Kanal Adı]

**[Konu Başlığı]**
...

Aynı kanalın birden fazla konusu tek bir "### [Kanal Adı]" başlığı altında toplanır; her konu için ayrı kanal başlığı açılmaz.
</şablon>"""

CONSOLIDATION_PROMPT = """<raporlar>
{combined_reports}
</raporlar>

<görev>
Yukarıda farklı kanallara ait içerik raporları var. Bunları aşağıdaki kurallarla tek bir konsolide rapor haline getir.
""" + _MERGE_RULES + """

Doğrudan "📌 ORTAK GÜNDEM" ile başla; öncesine hiçbir giriş cümlesi ekleme.
</görev>"""

FINAL_MERGE_PROMPT = """<batch_raporları>
{batch_reports}
</batch_raporları>

<görev>
Yukarıda kendi içinde birleştirilmiş grup raporları var. Bunları aşağıdaki kurallarla tek bir nihai rapora dönüştür. Farklı gruplarda aynı somut olayı anlatan konuları tek konuda birleştir: ortak bilgileri bir kez yaz, her kanalın özgün katkısını kendi satırında koru. Bir grupta tek kanalın işlediği bir konu başka grupta da işlenmişse artık ORTAK GÜNDEM'e taşınır.
""" + _MERGE_RULES + """

Doğrudan "📌 ORTAK GÜNDEM" ile başla; öncesine hiçbir giriş cümlesi ekleme.
</görev>"""


# İlgi testi: içerik raporu okuyucuya güncel ve ona ulaşan bir şey söylüyor mu?
# Tek bir karar sorusu (<karar>), altı ilgi kanalı (<tut>) ve atılacak kategoriler (<at>).
# Birden fazla konu işleyen içerik, konulardan biri testi geçiyorsa tutulur.
INTERESTS = """
<okuyucu>
Okuyucu Türkiye'de yaşıyor ve bu raporları kendisine ulaşabilecek gelişmeleri takip etmek için okuyor: Türkiye'nin siyaseti ve ekonomisi, komşu coğrafya, günün savaşları, kararları dünyaya yayılan büyük güçler ve dünyanın nereye gittiğini gösteren fikir ve toplum kaymaları. Bir içerik yerini yalnızca bu bağlardan biriyle ve güncel bir gelişme üzerinden kazanır. Ünlü bir isim, dramatik bir anlatım veya uzun bir video tek başına yetmez.
</okuyucu>

<karar>
Her içerik raporu için şu soruyu tek cümleyle cevapla: "Bu içerik okuyucuya Türkiye, bir savaş, bir büyük güç veya dünyanın gidişatı hakkında GÜNCEL olarak ne söylüyor?"
Somut bir cevap verebiliyorsan TUT. Dürüst cevap "güncel bir gelişme yok" veya "olayın kendisinden öte bir şey yok" ise AT.
Bir içerik birden fazla konu işliyorsa ve bunlardan EN AZ BİRİ bu testi geçiyorsa içeriği TUT.
</karar>

<tut>
1. TÜRKİYE, ulusal düzeyde: hükümet ve muhalefetin adımları, Meclis, ulusal ağırlıkta davalar ve mahkeme kararları, soruşturmalar, yolsuzluk ve ifşalar, ekonomi (faiz, kur, enflasyon, piyasalar, bütçe, büyük sektörler), dış politika, güvenlik, büyük düzenlemeler. Terör örgütlerine (PKK, DHKP-C, IŞİD ve diğerleri) yönelik güvenlik ve terörle mücadele operasyonları, ölçeği ne olursa olsun. Türkiye içinde binlerce kişiyi aynı anda etkileyen kitlesel olaylar (toplu hastalık, büyük afet). Son haftaların gelişmeleri.
2. SAVAŞ VE ÇATIŞMA, her yerde: cephe, saldırılar, ateşkes, müzakere, savaş hedefleri, yaptırımlar, savaşın yayılma riski ve savaşın gidişatına dair analizler.
3. BÜYÜK GÜÇLER (ABD, Çin, Rusya, AB ve büyük üyeleri, İngiltere, Hindistan, İsrail, İran, Suudi Arabistan): ülke çapında bir olay veya ulusal düzeyde bir karar. Seçimler ve seçmen havası, liderin tutumu ve gücü kullanma biçimi, hükümete karşı büyük mahkeme kararları, ekonomi, büyük düzenleme, hükümet krizi. Ülkenin yönünü sahadan somut gözlemlerle okuyan içerik buraya girer; tek bir kişinin portresi girmez. Bir partinin ne yapabileceğini tartan, bir seçimin neyi değiştireceğini tahmin eden veya bir anketi yeniden anlatan analiz olay değildir, atılır.
4. FİKİR VE EMSAL KAYMALARI: neyin meşru ve normal sayıldığını değiştiren olaylar, aktörler küçük olsa bile. Bir devletin başka bir ülkenin meşruiyetini reddetmesi, bir ideolojinin ırkçı ilan edilmesi, haklar, ifade, din veya sınırlar hakkında emsal niteliğinde bir karar, ilk kez çıkan bir yasa.
5. MODEL TOPLUMLARDA ÇATLAKLAR: gelişmiş ve istikrarlı bir ülkede yapısal bir sorunun ortaya çıkması. Bir kurum veya sektör içinde şiddet ve istismar, refah devletinin bir kesimi yüzüstü bırakması, güvenilen bir sistemin bozuk çıkması.
6. KÜRESEL EKONOMİ: petrol, altın, dolar, tahvil piyasaları, merkez bankaları, ticaret kuralları, yaptırımlar, IMF ve Dünya Bankası, yapay zekânın istihdama etkisi, iklim eşikleri. Türkiye'de fiyatları veya politikayı etkileyen her şey.
7. BÜTÜN DÜNYANIN OLAYLARI: nerede olursa olsun tüm dünyanın aynı anda izlediği az sayıda olay: Nobel ödülleri, BM Güvenlik Konseyi ve Genel Kurul kararları, G7, G20 ve COP zirveleri.
</tut>

<at>
- GÜNCEL OLMAYAN İÇERİK: genel teorik veya tarihsel anlatım ("finans kapital nedir", "Osmanlı nasıl çöktü"), felsefi ve ideolojik söyleşi, zamansız tartışma, güncel bir olaya bağlanmayan açıklayıcı veya eğitim içeriği. Güncel olay yalnızca giriş bahanesi olarak geçiyorsa içerik yine atılır.
- KİŞİSEL MESELELER: ünlü bir kişinin sağlığı, özel hayatı veya anıları; sanatçı, oyuncu, şarkıcı, sporcu söyleşileri; biyografik içerik; kişisel hikâye, hayata bakış, motivasyon sohbeti; tek bir kişinin kendi deneyimini anlattığı söyleşi.
- TEKİL OLAYLAR: tek bir kaza, suç, saldırı, gözaltı veya polis olayı, tek bir yerel proje (bir golf sahası, bir bina, bir park) üzerine tek bir dava, ulusal siyasete ulaşmıyorsa. Bu büyük güçler için de geçerlidir: devlet başkanının yenilemek istediği bir golf sahası hakkındaki mahkeme kararı yerel bir anlaşmazlıktır. Savaş bölgesindeki tekil olay SAVAŞ kapsamında kalır. Türkiye'de terörle mücadele operasyonu tekil olay sayılmaz; sıradan suç operasyonu (tefecilik, uyuşturucu, fuhuş) sayılır.
- YEREL VE KÜÇÜK ÖLÇEKLİ MESELELER: yerel bir olay, bir sektörün maliyet şikâyeti, bir partinin iç düzenlemesi, bir ilin sorunu, ulusal siyasi veya ekonomik ağırlığı yoksa. Binlerce kişiyi etkileyen olay ulusaldır ve tutulur.
- TEK SES: tek bir adayın kampanya mesajı, tek bir aday hakkındaki söylenti, gaf veya karalama ve bunun çökmesi, tek bir uzmanın bir sektör hakkındaki görüşü, güncel bir olaya bağlanmayan kişisel yorum. Seçimin durumu ilgilidir; içindeki tek bir ses veya tek bir olay değildir, yarış bir meclis çoğunluğunu belirleyecek olsa bile.
- GÜRÜLTÜ: spor, magazin, eğlence, lifestyle, sağlık tavsiyesi, reklam, sponsor içeriği, kanal duyurusu.
</at>
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


# Hedefli çekim (workflow girdileri): CHANNEL_FILTER = yalnızca bu kanal (CHANNELS'taki ad);
# LOOKBACK_HOURS = kaç saat geriye bakılacağı (varsayılan 24); LATEST_ONLY = kanal başına
# yalnızca en yeni uygun video. Kanal filtresi verilince köşe yazıları atlanır.
CHANNEL_FILTER = (os.getenv("CHANNEL_FILTER") or "").strip()
LOOKBACK_HOURS = float(os.getenv("LOOKBACK_HOURS") or 24)
LATEST_ONLY = (os.getenv("LATEST_ONLY") or "").strip().lower() in ("1", "true", "yes")


def get_latest_video_list():
    log("📡 YouTube'dan son videolar çekiliyor...")
    found_videos = []
    yesterday_dt = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    channels = {n: h for n, h in CHANNELS.items()
                if not CHANNEL_FILTER or n.casefold() == CHANNEL_FILTER.casefold()}
    if CHANNEL_FILTER and not channels:
        log(f"❌ CHANNEL_FILTER '{CHANNEL_FILTER}' CHANNELS içinde yok")
    for name, handle in channels.items():
        c_res = None
        taken = 0
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
                if pub_date > yesterday_dt and not (LATEST_ONLY and taken):
                    video_id = item["snippet"]["resourceId"]["videoId"]
                    short, duration = is_short(video_id)
                    if not short:
                        taken += 1
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
        f"<içerikler>\n{payload}\n</içerikler>\n\n"
        f"{INTERESTS}\n\n"
        "<karar> testini yukarıdaki her içerik raporuna uygula. "
        "ATILACAK içeriklerin indekslerini döndür; listelemediğin içerikler tutulur."
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


# PIPELINE_MODE=scrape: videoları/yazıları bul, transkriptleri indir, Gemini'ye ve Discord'a
# hiç dokunmadan hepsini CONTENT_FILE'a yaz ve çık. Rapor dışarıda üretilecekse kullanılır.
PIPELINE_MODE = (os.getenv("PIPELINE_MODE") or "full").strip().lower()
CONTENT_FILE = os.getenv("CONTENT_FILE", "manual/icerikler.json")


def scrape_only(videos):
    """Transkriptleri indir ve Gemini'siz olarak dosyaya yaz."""
    out = []
    for i, video in enumerate(videos, 1):
        log(f"[{i}/{len(videos)}] Transkript: [{video['name']}] {video['title']}")
        transkript = video.get("transkript") or transkript_cek(video["video_id"])
        if not transkript:
            log(f"⚠️ Transkript alınamadı, içerik atlanıyor", indent=1)
            continue
        out.append({
            "name": video["name"], "title": video["title"], "duration": video.get("duration", ""),
            "url": video.get("url", ""), "kind": video.get("kind", "video"),
            "video_id": video.get("video_id", ""), "transkript": transkript,
        })
    os.makedirs(os.path.dirname(CONTENT_FILE) or ".", exist_ok=True)
    with open(CONTENT_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": datetime.now(ISTANBUL_TZ).strftime("%d.%m.%Y"), "items": out},
                  f, ensure_ascii=False, indent=1)
    log(f"📝 {len(out)}/{len(videos)} içerik {CONTENT_FILE} dosyasına yazıldı "
        f"({sum(len(x['transkript']) for x in out):,} karakter)")


if __name__ == "__main__":
    run_start = time.time()
    log(f"🚀 BAŞLATILIYOR" + (f" (mod: {PIPELINE_MODE})" if PIPELINE_MODE != "full" else ""))

    videos = get_latest_video_list()
    if not CHANNEL_FILTER:
        videos += get_latest_articles()

    if not videos:
        log("Son 24 saatte yeni video/yazı bulunamadı.")
    elif PIPELINE_MODE == "scrape":
        scrape_only(videos)
    elif PIPELINE_MODE != "full":
        raise SystemExit(f"Bilinmeyen PIPELINE_MODE '{PIPELINE_MODE}' (full veya scrape)")
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
            pdf_name = _safe_filename(f"Youtube Gündem Raporu {current_date}") + ".pdf"
            try:
                pdf_bytes = build_pdf_report(full_report, f"Youtube Gündem Raporu — {current_date}")
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
