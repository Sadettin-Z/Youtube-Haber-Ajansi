import io
import json
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
    "Murat Yetkin": "@muratyetkinreport",
    "Atilla Yeşilada": "@atillayesilada",
    "Mesele Ekonomi": "@MeseleEkonomi",
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


# Tek adımda rapor: ham transkript doğrudan konu konu habere çevrilir (önce temizleme yok).
# Ölçüt haber raporuyla aynıdır: konu bazında ilgi süzgeci, önce konunun özü, sonra belirleyici
# ayrıntılar birebir; metinde olmayan hiçbir şey eklenmez. Sohbet, duyuru ve reklam atılır.
REPORT_PROMPT = """<transkript>
{transkript}
</transkript>

<görev>
Yukarıdaki metin, {source_line} Ham, otomatik çevrilmiş bir transkripttir: noktalama eksiktir, konuşmacılar ayrılmamıştır, isimler bozuk yazılmış olabilir. {commentator_line}

Bu metinden, içeriği izlemeyen bir okuyucu için konu konu bir haber raporu yaz. Rapor tek adımda yazılır: önce metni temizlemez, özetlemez, doğrudan raporu yazarsın.

<okuyucu>
Okuyucu Türkiye'de yaşıyor; dünyada olup bitenlerden kendisine ulaşabilecek olanları takip ediyor: Türkiye'nin siyaseti, ekonomisi, güvenliği ve yargısı; savaşlar; büyük güçlerin ülke çapındaki kararları ve devletlerin birbirine yönelik adımları; dünya ölçeğinde ekonomi ve teknoloji kaymaları.
</okuyucu>

<adım_1_konular>
Metni konulara ayır. Konu, belirli bir olay, karar, açıklama, veri, dava, soruşturma ya da yorumcunun belirli bir iddiasıdır. Her konu için karar ver: rapora girer mi?

GİRER:
- Türkiye: ulusal siyaset, hükümet ve muhalefetin adımları, Meclis, ekonomi ve geçim (fiyatlar, üretici, ücretler, enflasyon, kur), güvenlik ve terörle mücadele, yargı, ulusal ağırlıkta dava ve soruşturmalar, yolsuzluk ve ifşalar, devlet kurumlarının eylemleri, ulusal figürlerin durumu ve açıklamaları, dış politika. Türkiye'nin ya da bir Türk aktörün geçtiği her yabancı konu.
- Savaşlar ve çatışmalar: cephe, saldırı, ateşkes, müzakere, yaptırım.
- Büyük güçler (ABD, Çin, Rusya, AB ve büyük üyeleri, İngiltere, İsrail, İran, Suudi Arabistan ve Körfez, Hindistan): ülke çapında olay ve karar, seçim, hükümet krizi, devletlerin birbirine yönelik adımları.
- Dünya ölçeğinde ekonomi ve teknoloji: petrol, altın, dolar, faiz, merkez bankaları, ticaret, yapay zekâ yarışı ve en büyük şirketlerin bir savaş, kriz ya da teknoloji kaymasının etkisi olarak okunan sonuçları.
- Yorumcunun bu konulardaki kendi bilgileri, kulis bilgileri, iddiaları, soruları ve değerlendirmeleri: bunlar ait oldukları konunun parçasıdır.

GİRMEZ:
- Sohbet, selamlaşma, şakalar, sunucular arası takılmalar; yayın akışına dair sözler ("görüntüyü verelim", "buna sonra geleceğiz").
- Kanal ve program duyuruları; abonelik, beğeni, bağış ve destek çağrıları (Patreon, Kreosus, YouTube katıl); sponsor ve reklam.
- Spor, magazin, eğlence, dizi ve film, yaşam tarzı, sağlık ve tüketici tavsiyesi.
- Tarih ve arka plan dersleri; bir kişinin kariyer geçmişi ve biyografisi; yorumcunun anıları ve kişisel hikâyeleri; profiller. Güncel bir konunun anlaşılması için gereken bağlam en fazla bir cümle olarak o konunun içinde kalır.
- Türkiye dışında ve bir savaş dışında kalan tekil olaylar: tek bir kaza, suç, saldırı, gözaltı, dava. Büyük güç olmayan ve Türkiye'nin bölgesinde (Orta Doğu, Körfez, Balkanlar, Kafkasya, Karadeniz) olmayan bir ülkenin iç siyaseti.
- Belirli bir konuya bağlanmayan duygu boşaltma, slogan, temenni, hakaret ve kehanet. Yorumcunun bir sözü ancak belirli bir konu hakkında belirli bir iddia, soru ya da argüman taşıyorsa o konunun içinde yer alır.
- Aynı bilginin tekrarı: yorumcu bir şeyi birkaç kez söylüyorsa bir kez yazılır.
</adım_1_konular>

<adım_2_yazım>
Giren her konu için bir başlık ve tek bir paragraf yaz.

Başlık ("### " ile başlar): en çok 15 kelime; somut aktör + somut eylem + konudaki en çarpıcı unsur (beklenmedik sonuç, çelişki, rakam, karşı karşıya geliş). Aktif fiil, en önemli bilgi başta. Bilgiyi saklayan kalıplar ("Bakın ne oldu"), ünlem, BÜYÜK HARF vurgusu, cevapsız soru başlığı ve "şok", "bomba", "tarihi" gibi sıfatlar kullanılmaz. Başlık yorumcunun iddiasını ya da sorusunu taşıyorsa bu görünür: "Atay: örgütün adı ne?"

Paragraf: 2 ile 6 cümle arası, akan düz yazı; madde işareti, alt başlık, etiket ("Yorum:") yok. Önce konunun özü (ne oldu, kim yaptı, neden önemli), sonra onsuz anlaşılmayacak belirleyici ayrıntılar: sayılar, tarihler, isimler, görevler, alıntılar. Bunlar metindeki haliyle birebir kalır; doğrudan alıntı tırnak içinde verilir. Bir kişi ilk geçtiğinde metinde söylenen göreviyle tanıtılır ("Adalet Bakanı Akın Gürlek"); görev metinde geçmiyorsa uydurulmaz. Başkasının sözü, açıklaması ya da iddiası o kişinin adıyla verilir ("Bahçeli ... dedi"). Metinde iddia olarak geçen şey raporda da iddia olarak kalır.

Yorumcunun kendi bilgisi, iddiası, sorusu ve değerlendirmesi ile aktardığı olay birbirine karışmaz: yorumcuya ait olanlar soyadıyla bağlanır ("Atay'a göre ...", "Atay ... diye sordu", "Atay ... dedi"); ama her cümle bu bağlaçla başlamaz, bir konunun yorum kısmı birkaç cümle sürüyorsa bağlaç bir kez yeter. Yorumcunun benzetmeleri ve süslü sözleri ("deprem", "fitili ateşledi") kopyalanmaz; arkasındaki somut iddia metinde söyleniyorsa o yazılır, söylenmiyorsa atılır. Yorumcunun argümanı yumuşatılmaz, sertleştirilmez, çarpıtılmaz.

Metinde olmayan hiçbir bilgi, bağlam, görev, tarih, sebep, sonuç ya da değerlendirme eklenmez; kendi bilgin ve kendi yorumun rapora girmez. Bağlaç ("çünkü", "bu yüzden", "ancak") yalnızca metinde o ilişki kuruluyorsa kullanılır.

İsimler: kamuoyunda bilinen bir kişi ya da kurumun kastedildiği bağlamdan kesinse doğru yazımı kullan (transkriptte "Akın Gürlü'e" geçiyorsa Akın Gürlek, "Musavat Derbeşoğlu" geçiyorsa Müsavat Dervişoğlu); kesin değilse transkriptteki yazımı koru.

Aktarım fiilleri sade: "dedi", "söyledi", "diyor", "iddia ediyor", "soruyor", "-e göre". "Belirtmektedir", "ifade etmektedir", "vurgulamaktadır" kullanılmaz.

Dil: metnin dili (Türkçe ise Türkçe). Konular metindeki sırayla yazılır. Konular arasına tek başına bir "---" satırı konur.
</adım_2_yazım>

<örnek>
<örnek_transkript>
Fon soruşturması kapsamında 31 Ekim'de gözaltına alınan eski Borsa İstanbul AŞ Genel Müdürü Korkmaz Enes Ergün rüşvet almak suçlamasıyla sevk edildiği sufh Ceza Mahkemeliğince tutuklandı. >> Devam edecek. Daha devamı edecek. Dediğimiz gibi devletin içerisinde, kurumların içerisinde birçok kişi görevden alınacak ve birçok kişi de yavaş yavaş ya gözaltı ya da tutuklamalar yapılacak. Buraya kadar normal. Ama tutuklanmayanlar, sessiz bırakılanlar, başka görevlere gönderilenler bunlara dokunulacak mı? Kirlilikse bu adam kirliyse kardeşim diğer adamları da yakalayacaksınız. Ayrım yok. Hukuk dediğin şey herkes hukuk karşısında eşittir. Ben 3 liraya çaldım, ötekisi 5 lira çaldı. Yani kardeşim beni alıyorsun da o 5 lira çalanı niye almıyorsun sorusunu sordurtmayacaksınız bu milleti. Bu milletin artık dayanacak gücü kalmadı. Tabii desteklerinizi bekliyoruz değerli dostlar. Eğer varsa Kreosus'tan, Patreon'dan gücü yetenler bizlere destek olabilirse çok memnun oluruz. Programı beğenmeyi mutlaka yapın.
</örnek_transkript>
<doğru_rapor>
### Eski Borsa İstanbul Genel Müdürü Korkmaz Enes Ergün rüşvetten tutuklandı

Fon soruşturması kapsamında 31 Ekim'de gözaltına alınan eski Borsa İstanbul AŞ Genel Müdürü Korkmaz Enes Ergün, rüşvet almak suçlamasıyla sevk edildiği sulh ceza hâkimliğince tutuklandı. Atay'a göre devletin ve kurumların içinde birçok kişi daha görevden alınacak, gözaltılar ve tutuklamalar sürecek; asıl soru tutuklanmayanlara, sessiz bırakılanlara ve başka görevlere gönderilenlere dokunulup dokunulmayacağı. "Ben 3 liraya çaldım, ötekisi 5 lira çaldı; beni alıyorsun da o 5 lira çalanı niye almıyorsun" sorusunun bu millete sordurulmaması gerektiğini söyledi.
</doğru_rapor>
Destek ve beğeni çağrısı rapora girmedi; "sufh Ceza Mahkemeliğince" bozuk yazımı düzeltildi; yorumcunun değerlendirmesi olaya "Atay'a göre" ile bir kez bağlandı.
</örnek>

<çıktı>
Raporu <rapor> ... </rapor> arasına yaz; öncesine ve sonrasına hiçbir şey ekleme. Rapora giren konu yoksa <rapor>BOŞ</rapor> döndür.
</çıktı>
</görev>"""

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


def call_gemini(prompt, label="Gemini", response_schema=None, rounds=3):
    """Shared helper: returns text or None.

    Runs on MODEL_CHAIN[0], trying each key in order. A key whose daily quota on that model is
    spent is retired for that model; a 403/invalid key is retired on every model. When a model
    fails on every key (quota spent, rate limit or a transient 503 "high demand"), the call moves
    to the next model in MODEL_CHAIN. When every model failed and at least one failure was
    transient, the whole chain is tried again after a wait, up to `rounds` times. If
    response_schema is provided, the model is forced to return JSON."""
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

    for round_no in range(1, rounds + 1):
        retry_later = False  # a transient or rate-limit failure happened in this round
        for m_pos, model in enumerate(MODEL_CHAIN):
            keys = _live_keys(model)
            if not keys:
                continue  # quota spent or keys dead on this model — try the next one
            log(f"🧠 Model: {model} ({len(keys)} kullanılabilir key, tur {round_no}/{rounds})", indent=2)

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
                            retry_later = True
                            log(f"🐢 Key #{key_idx} dakikalık limit ({elapsed:.1f}s), sıradaki key'e geçiliyor", indent=2)
                            break
                        if kind == "transient":
                            retry_later = True
                            log(f"🔁 Key #{key_idx} geçici hata ({elapsed:.1f}s): {err_str[:200]} — sıradaki key'e geçiliyor", indent=2)
                            break
                        wait = (attempt + 1) * 30
                        log(f"❌ API hatası ({elapsed:.1f}s, deneme {attempt+1}/3): {err_str[:200]}", indent=2)
                        log(f"⏳ {wait} saniye bekleniyor...", indent=2)
                        time.sleep(wait)

            if m_pos + 1 < len(MODEL_CHAIN):
                log(f"➡️ '{model}' bu turda yanıt vermedi, '{MODEL_CHAIN[m_pos + 1]}' modeline geçiliyor", indent=1)

        if not retry_later or round_no == rounds:
            break
        wait = 60 * round_no
        log(f"⏳ Tüm modeller geçici hata verdi; {wait} saniye sonra tur {round_no + 1}/{rounds}", indent=1)
        time.sleep(wait)

    log(f"❌ [{label}] Tüm key'ler ve denemeler tükendi", indent=1)
    return None


_NON_PERSON_CHANNEL = re.compile(r"\b(tv|report|haber|ajans|kanal|ekonomi)\b", re.IGNORECASE)


def _commentator_line(video):
    """Tells the model whom to attribute the commentator's own claims to."""
    name = video["name"].split("(")[0].strip()
    if _NON_PERSON_CHANNEL.search(name):
        return ("Kanal bir kişi değil; konuşanın adı başlıktan ya da metinden anlaşılıyorsa "
                "yorumcunun sözlerini o kişinin soyadıyla bağla, anlaşılmıyorsa \"yorumcu\" de.")
    surname = name.split()[-1]
    return f"Yorumcu {name}; yorumcunun kendi sözlerini \"{surname}\" soyadıyla bağla."


def analyze_video(video, transkript):
    """One Gemini call per content: raw transcript in, topic-by-topic report out.

    Returns the report text, "" when the model found no topic that passes the
    relevance test (<rapor>BOŞ</rapor>), or None when every API key failed."""
    if video.get("kind") == "article":
        source_line = (f'{video["name"]} imzasıyla yayımlanan "{video["title"]}" '
                       f'başlıklı köşe yazısının tam metnidir.')
    else:
        source_line = (f'{video["name"]} kanalında yayınlanan "{video["title"]}" '
                       f'başlıklı videonun transkriptidir.')
    prompt = REPORT_PROMPT.format(source_line=source_line, transkript=transkript,
                                  commentator_line=_commentator_line(video))
    result = call_gemini(prompt, label=f"{video['name'][:20]} rapor")
    if result is None:
        return None
    m = re.search(r"<rapor>(.*?)(?:</rapor>|$)", result, re.DOTALL)
    report = (m.group(1) if m else result).strip()
    if not report or report.upper() == "BOŞ":
        return ""
    return report


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


def write_contents(items):
    """Write the fetched transcripts to CONTENT_FILE (the raw material of the report)."""
    out = [{
        "name": v["name"], "title": v["title"], "duration": v.get("duration", ""),
        "url": v.get("url", ""), "kind": v.get("kind", "video"),
        "video_id": v.get("video_id", ""), "transkript": v["transkript"],
    } for v in items]
    os.makedirs(os.path.dirname(CONTENT_FILE) or ".", exist_ok=True)
    with open(CONTENT_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": datetime.now(ISTANBUL_TZ).strftime("%d.%m.%Y"), "items": out},
                  f, ensure_ascii=False, indent=1)
    log(f"📝 {len(out)} içerik {CONTENT_FILE} dosyasına yazıldı "
        f"({sum(len(x['transkript']) for x in out):,} karakter)")


def scrape_only(videos):
    """Transkriptleri indir ve Gemini'siz olarak dosyaya yaz."""
    fetched = []
    for i, video in enumerate(videos, 1):
        log(f"[{i}/{len(videos)}] Transkript: [{video['name']}] {video['title']}")
        transkript = video.get("transkript") or transkript_cek(video["video_id"])
        if not transkript:
            log(f"⚠️ Transkript alınamadı, içerik atlanıyor", indent=1)
            continue
        video["transkript"] = transkript
        fetched.append(video)
    write_contents(fetched)


def build_full_report(analyzed, current_date):
    """Header + one section per content, each with its topic-by-topic report."""
    videos_list = "\n".join(
        f"- **[{it['video']['name']}]** {it['video']['title']} `({it['video']['duration']})`"
        for it in analyzed
    )
    sections = []
    for idx, it in enumerate(analyzed, 1):
        v = it["video"]
        sections.append(
            f"## {idx}. [{v['name']}] {v['title']} `({v['duration']})`\n\n"
            f"{v.get('url', '')}\n\n"
            f"{it['report']}"
        )
    return (
        f"📅 **{current_date}**\n\n"
        f"📺 **İncelenen İçerikler ({len(analyzed)} adet):**\n"
        f"{videos_list}\n\n"
        f"---\n\n"
        + "\n\n---\n\n".join(sections)
        + "\n"
    )


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

        analyzed = []   # her eleman: {"video": video, "report": report}
        fetched = []    # transkripti alınan her içerik (CONTENT_FILE için)
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
            video["transkript"] = transkript
            fetched.append(video)

            log(f"📝 İlk 300 karakter: {transkript[:300]}...", indent=1)
            report = analyze_video(video, transkript)
            video_elapsed = time.time() - video_start
            if report is None:
                log(f"❌ Rapor üretilemedi (tüm API key'leri tükendi), içerik atlanıyor ({video_elapsed:.1f}s)", indent=1)
            elif report == "":
                log(f"⊘ Rapora girecek konu yok, içerik atlanıyor ({video_elapsed:.1f}s)", indent=1)
            else:
                analyzed.append({"video": video, "report": report})
                log(f"✅ İçerik tamamlandı ({video_elapsed:.1f}s, rapor: {len(report):,} karakter)", indent=1)
            time.sleep(2)

        if fetched:
            write_contents(fetched)

        if not analyzed:
            log("❌ Rapora girecek içerik yok, rapor üretilmiyor.")
        else:
            current_date = datetime.now(ISTANBUL_TZ).strftime("%d.%m.%Y")
            full_report = build_full_report(analyzed, current_date)

            report_path = os.path.join(
                os.path.dirname(CONTENT_FILE) or ".",
                f"Youtube Gündem Raporu {datetime.now(ISTANBUL_TZ).strftime('%Y-%m-%d')}.md")
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(full_report)
            log(f"📝 Rapor dosyaya yazıldı: {report_path} ({len(full_report):,} karakter)")

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
            log(f"   • {len(analyzed)}/{len(fetched)} içerik rapora girdi")
            log(f"   • Toplam süre: {mins} dk {secs} sn")
            log(f"   • Rapor Discord'a gönderildi (metin + PDF)")
