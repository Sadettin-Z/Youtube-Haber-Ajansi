"""Twitter (X) Haber Ajansı.

Takip edilen X hesaplarının son paylaşımlarını Apify üzerinden çeker, Gemini ile
gürültüyü (espri, kutlama, reklam, kişisel sohbet, tepki paylaşımları) eler, haber
değeri taşıyan paylaşımları konu konu birleştirip Discord'a gündem raporu gönderir.

Akış:
  1. Hesap listesi: twitter/accounts.txt (elle) > twitter/followings.json (önbellek)
     > Apify ile X_HANDLE hesabının takip ettikleri (haftada bir yenilenir).
  2. Apify tweet scraper ile son WINDOW_HOURS saatin paylaşımları.
  3. Gemini ELEME: her paylaşım için "haber mi, gürültü mü" + tek cümle özet.
  4. Gemini RAPOR: kalan paylaşımlar konu bazında birleştirilir.
  5. Discord'a gönderilir, rapor twitter/reports/ altına yazılır.

Ortam değişkenleri (GitHub Secrets / Variables):
  APIFY_API_KEY, GEMINI_API_KEY_1..3, DISCORD_WEBHOOK_URL       (zorunlu)
  X_HANDLE                takip listesi çekilecek hesap, ör. "SadettinZ_"
  WINDOW_HOURS            kaç saat geriye bakılacak (varsayılan 24; haftalık için 168)
  MAX_TWEETS_PER_ACCOUNT  hesap başına en fazla paylaşım (varsayılan 10)
  MAX_TWEETS_TOTAL        koşu başına toplam paylaşım tavanı (varsayılan 300)
  MONTHLY_PAID_ITEMS_BUDGET aylık ücretli kayıt bütçesi (varsayılan 9000 ≈ 1,35 $); dolunca ücretli aktörler atlanır
  FREE_RUNS_PER_MONTH     ücretsiz aktörlerin aylık koşu hakkı (varsayılan 5); 0 = ücretsizi hiç deneme
  INCLUDE_REPLIES         "1" ise yanıtlar da alınır (varsayılan 0)
  FOLLOWINGS_REFRESH_DAYS takip listesi kaç günde bir yenilenir (varsayılan 30)
  REFRESH_FOLLOWINGS      "1" ise önbelleğe bakmadan takip listesini yeniden çek
  DISCORD_MENTION_USER_ID raporun başında etiketlenecek Discord kullanıcı id'si (isteğe bağlı)
  PIPELINE_MODE           full (varsayılan) | scrape (sadece tweetleri indir) | probe (aktörleri sına)

Aktör zinciri: DEFAULT_ACTORS (aşağıda) veya twitter/actors.json. Aylık sayaçlar: twitter/state.json.
"""

import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "keys", ".env"), override=True, encoding="utf-8-sig")
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Ayarlar
# ---------------------------------------------------------------------------

APIFY_API_KEY = os.getenv("APIFY_API_KEY")
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL")
DISCORD_MENTION_USER_ID = (os.getenv("DISCORD_MENTION_USER_ID") or "").strip()
GEMINI_API_KEYS = [k for k in (os.getenv(f"GEMINI_API_KEY_{i}") for i in range(1, 4)) if k]
if not GEMINI_API_KEYS:
    _fallback = os.getenv("GEMINI_API_KEY")
    if _fallback:
        GEMINI_API_KEYS = [_fallback]

MODEL_CHAIN = [m.strip() for m in os.getenv(
    "GEMINI_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash").split(",") if m.strip()]
_dead_keys = {}
_quota_dead = set()

X_HANDLE = (os.getenv("X_HANDLE") or "").strip().lstrip("@")
WINDOW_HOURS = int(os.getenv("WINDOW_HOURS") or 24)
MAX_TWEETS_PER_ACCOUNT = int(os.getenv("MAX_TWEETS_PER_ACCOUNT") or 10)
# Bir koşuda ücretli aktörden istenecek toplam paylaşım üst sınırı (maliyet freni).
MAX_TWEETS_TOTAL = int(os.getenv("MAX_TWEETS_TOTAL") or 300)
# Aylık ücretli kayıt bütçesi. Apify kredisi başka aktörlerle paylaşıldığı için düşük tutulur.
# 9000 kayıt ≈ 1,35 $ (xquik 0,15 $/1000). Dolunca ücretli aktörler ay sonuna kadar atlanır.
MONTHLY_PAID_ITEMS_BUDGET = int(os.getenv("MONTHLY_PAID_ITEMS_BUDGET") or 9000)
# Ücretsiz (free:true) aktörlerin aylık koşu hakkı. apidojo aktörleri: ayda 5 koşu, koşu başına 10 kayıt.
FREE_RUNS_PER_MONTH = int(os.getenv("FREE_RUNS_PER_MONTH") or 5)
INCLUDE_REPLIES = (os.getenv("INCLUDE_REPLIES") or "0").strip() in ("1", "true", "yes")
FOLLOWINGS_REFRESH_DAYS = int(os.getenv("FOLLOWINGS_REFRESH_DAYS") or 30)
REFRESH_FOLLOWINGS = (os.getenv("REFRESH_FOLLOWINGS") or "0").strip() in ("1", "true", "yes")
# Tek Apify koşusuna verilecek hesap sayısı. Küçük tutmak koşuları kısa ve hatayı yerel tutar.
HANDLES_PER_RUN = int(os.getenv("HANDLES_PER_RUN") or 25)
PIPELINE_MODE = (os.getenv("PIPELINE_MODE") or "full").strip().lower()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "twitter")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.txt")
FOLLOWINGS_FILE = os.path.join(DATA_DIR, "followings.json")
TWEETS_FILE = os.path.join(DATA_DIR, "tweets.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
ACTORS_FILE = os.path.join(DATA_DIR, "actors.json")
PROBE_DIR = os.path.join(DATA_DIR, "probe")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")

# ---------------------------------------------------------------------------
# Apify aktör zincirleri. Sırayla denenir; ücretsiz aktörün aylık hakkı bitince, koşu hata verince
# veya sonuç "cap" sayısına takılınca (eksik sonuç) sıradaki aktöre geçilir.
# twitter/actors.json varsa bu varsayılanların yerine geçer (aynı yapı: {"tweets": [...], "followings": [...]}).
#   kind=handles : apidojo tarzı {"twitterHandles": [...], "maxItems", "sort", "start", "end"}
#   kind=search  : {"searchTerms": ["from:h since:... -filter:replies", ...], "maxItems", ...}
#   kind=user_scraper : apidojo twitter-user-scraper {"twitterHandles": [h], "getFollowing": true}
#   kind=userId  : {"userId": "h"}
#   kind=urls    : {"urls": ["https://x.com/h/following"], "maxResults"}
#   "input": aktöre aynen eklenecek ek alanlar.  "free": ücretsiz koşu hakkına tabi.  "cap": koşu başına tavan.
# ---------------------------------------------------------------------------
DEFAULT_ACTORS = {
    "tweets": [
        {"actor": "apidojo~tweet-scraper", "kind": "handles", "free": True, "cap": 10,
         "input": {"sort": "Latest", "includeSearchTerms": False}},
        # xquik: maxItems'a uyuyor, 0,15 $/1000, her planda tam sonuç (probe: 5 istendi 5 geldi).
        {"actor": "xquik~x-tweet-scraper", "kind": "search", "input": {}},
        # kaitoeasyapi: maxItems'ı yok sayıyor (probe: 5 istendi 40 geldi) → maliyeti öngörülemez, son çare.
        {"actor": "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest", "kind": "search",
         "input": {"queryType": "Latest"}},
    ],
    "followings": [
        {"actor": "apidojo~twitter-user-scraper", "kind": "user_scraper", "free": True, "cap": 10, "input": {}},
        # data-slayer: 1,5 $/1000 → 35 hesap ≈ 0,05 $ (probe: 35 kayıt, tam liste).
        {"actor": "data-slayer~twitter-followings", "kind": "userId", "input": {}},
        # api-ninja: koşu başına asgari ücret var; yedek (probe: 35 kayıt, tam liste).
        {"actor": "api-ninja~x-twitter-followers-scraper", "kind": "urls", "input": {"scrapeAllResults": False}},
    ],
}


def load_actors():
    if os.path.exists(ACTORS_FILE):
        try:
            with open(ACTORS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("tweets") or data.get("followings"):
                return {"tweets": data.get("tweets") or DEFAULT_ACTORS["tweets"],
                        "followings": data.get("followings") or DEFAULT_ACTORS["followings"]}
        except Exception as e:
            log(f"⚠️ actors.json okunamadı, varsayılanlar kullanılıyor: {e}")
    return DEFAULT_ACTORS


# ---------------------------------------------------------------------------
# Aylık durum: ücretsiz koşu sayacı ve ücretli kayıt sayacı (twitter/state.json, repoya commit edilir).
# ---------------------------------------------------------------------------

def _month_key():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def load_state():
    st = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
        except Exception as e:
            log(f"⚠️ state.json okunamadı: {e}")
            st = {}
    if st.get("month") != _month_key():
        st = {"month": _month_key(), "free_runs": {}, "paid_items": 0, "paid_runs": 0, "notes": []}
    st.setdefault("free_runs", {}); st.setdefault("paid_items", 0); st.setdefault("paid_runs", 0); st.setdefault("notes", [])
    return st


def save_state(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


STATE = None  # main() içinde yüklenir
# run_chain, ücretli aktörler çalışmayıp ücretsiz aktörün kesik sonucu kullanıldığında bu notu ekler.
FALLBACK_NOTE = "ücretli aktör çalışmadı, kesik ücretsiz sonuç kullanılıyor"


def _free_runs_left(spec):
    return FREE_RUNS_PER_MONTH - STATE["free_runs"].get(spec["actor"], 0)


def _paid_budget_left():
    return MONTHLY_PAID_ITEMS_BUDGET - STATE["paid_items"]

# Türkiye kalıcı olarak UTC+3.
ISTANBUL_TZ = timezone(timedelta(hours=3))

# ELEME aşamasında bir Gemini çağrısına verilecek paylaşım sayısı. 120'lik batch 35k karakter prompt ve
# 4-5 dakikalık yanıt üretti (bağlantı kopması riski); 60 ile çağrılar kısa kalır.
TRIAGE_BATCH = 60
# RAPOR aşamasında tek çağrıya sığacak haber sayısı; üstü batch'lenir.
REPORT_BATCH = 80


def ts():
    return datetime.now(ISTANBUL_TZ).strftime("%H:%M:%S")


def log(msg, indent=0):
    print(f"[{ts()}] {'  ' * indent}{msg}", flush=True)


# ---------------------------------------------------------------------------
# Promptlar
# ---------------------------------------------------------------------------

TRIAGE_PROMPT = """<paylaşımlar>
{payload}
</paylaşımlar>

<görev>
Yukarıda X (Twitter) paylaşımları var. Her biri "[indeks] @hesap (Ad) | ❤️ beğeni 🔁 repost | metin | link" biçiminde.
Görevin: haber değeri taşıyanları ayıklamak, gürültüyü atmak.

TUT (haber değeri var):
- Somut güncel olay: karar, açıklama, atama, istifa, operasyon, dava, mahkeme kararı, yasa/yönetmelik, ihale, anlaşma
- Türkiye siyaseti, dış politika, ekonomi, piyasalar (faiz, kur, enflasyon, borsa), enerji, savunma
- Uluslararası gelişmeler: savaş, müzakere, seçim, yaptırım
- Belge, veri, rakam, rapor paylaşımı; soruşturma ve yolsuzluk iddiaları; ifşa
- Bir gazetecinin/uzmanın somut bir güncel olaya dair analizi veya iddiası
- Duyurulan yeni politika, proje, teknoloji veya bilimsel gelişme (somutsa)

AT (gürültü):
- Selfie, kutlama, taziye, günaydın/iyi geceler, "teşekkürler", doğum günü, bayram mesajı
- Espri, mizah, meme, emoji-ağırlıklı tepkiler, taraftar/spor sohbeti, magazin
- Reklam, sponsor, çekiliş, abonelik/etkinlik duyurusu, "yayındayız" tipi program duyuruları (içeriği söylenmiyorsa)
- Kişisel sohbet, cevap tartışması, hakaret, slogan, boş motivasyon
- Sadece bağlantı ve başlıksız/bağlamsız metin; tek kelimelik tepkiler
- Aynı haberin anlamsız tekrarı (ilk görüleni tut, tekrarları at)

ÖNEMLİ: Tereddütteysen TUT. Bir paylaşım hem gürültü hem bilgi içeriyorsa bilgi için TUT.
Program/yayın duyurusu konuyu açıkça söylüyorsa (ör. "bu akşam X'in fon dosyasını konuşuyoruz") TUT: konu, gündem bilgisidir.

Her TUTULAN paylaşım için:
- index: paylaşımın indeksi
- topic: 3-8 kelimelik somut konu etiketi (aynı olayı anlatan paylaşımlara AYNI etiketi ver; "Ekonomi" gibi soyut etiket yok, "Merkez Bankası faiz kararı" gibi somut)
- summary: paylaşımın ne söylediğini 1-2 cümlede, kendi yorumunu katmadan, metindeki sayı/isim/iddiayı koruyarak özetle. Metin Türkçe değilse Türkçeye çevirerek özetle.

Sadece JSON döndür.
</görev>"""

REPORT_PROMPT = """<haberler>
{payload}
</haberler>

<görev>
Yukarıda, {window_label} içinde takip edilen X (Twitter) hesaplarından gelen ve haber değeri taşıdığı belirlenen paylaşımlar var. Her satır "[indeks] @hesap (Ad) | konu etiketi | ❤️ beğeni 🔁 repost | özet | link" biçiminde.
Bunlardan tek bir gündem raporu üreteceksin.

Önemli kural 1 — DIŞ BİLGİ YOK: Sadece yukarıdaki paylaşımlardaki bilgiyi kullan. Eğitim verinden rol, tarih, arka plan, açıklama ekleme. Girdide olmayan şey raporda yok.

Önemli kural 2 — TEKRAR YOK, SADECE ÖNEMLİ OLAN: Aynı bilgiyi iki kez yazma. Her konuda özü ve en önemli bir-iki detayı (karar, sayı, isim, iddia) tut. Şüphedeysen detayı at. Koruduğun sayı, isim ve alıntılar girdideki haliyle birebir doğru olsun.

Önemli kural 3 — ÇIPLAK BİLGİ: Metafor ve süs sıfatı ("şok", "tarihi", "müthiş", "deprem") yok; sıfat varsa onu doğuran somut bilgi cümlede olmalı. Pasif aktarım fiilleri ("belirtmektedir", "ifade etmektedir", "vurgulamaktadır") yasak. Aktarım gerekiyorsa düz olsun: "Akinan X dedi", "Yetkin'e göre Y".

Önemli kural 4 — KAYNAK LİNKİ: Her konunun sonunda o konuyu oluşturan paylaşımlardan en fazla 3 link ver; linkleri köşeli parantez içinde < > ile sar: <https://x.com/...>. Link uydurma, sadece girdideki linkleri kullan.

Yapı:
Aynı somut olayı (aynı karar, aynı açıklama, aynı dava) birden fazla hesap paylaşmışsa "📌 ORTAK GÜNDEM" bölümüne; tek bir hesap paylaşmışsa "🐦 HESAPLARA ÖZEL" bölümüne koy. Aynı kişi adının geçmesi birleştirmek için yeterli değil; olay aynı olmalı. Şüphedeysen ayrı tut.
ORTAK GÜNDEM'i kapsama göre sırala: en çok hesabın paylaştığı konu en üstte.
HESAPLARA ÖZEL'de aynı hesabın konuları tek "### @hesap" başlığı altında toplanır; en çok beğeni/repost alan paylaşımlar önce gelsin. Küçük ve birbirine yakın konuları tek maddede birleştir; önemsiz tek satırlık haberleri at.

Başlık kuralı: somut aktör + somut eylem + çarpıcı unsur (rakam, çelişki, cepheleşme). Aktif fiil, en önemli bilgi başta. Clickbait, ünlem, BÜYÜK HARF ve cevapsız soru başlığı yasak.
Örnek — Düz: "Merkez Bankası Faiz Kararı" / Doğru: "Beklenti 100 Baz Puandı: Merkez Bankası 250 İndirdi, Dolar 36'yı Aştı"

Şablonu birebir kullan:

📌 ORTAK GÜNDEM

### [Spesifik Konu Başlığı]
**Ortak Bilgiler:** Olayın özü ve hesapların ortaklaştığı en önemli bilgiler BİR KEZ, kısa tek paragraf. Bilginin sahibi cümlede geçer ("Akinan ve Özdil X dedi").
**Hesap Katkıları:**
- **@hesap (Ad):** SADECE bu hesaba özgü iddia/sayı/belge. Ortak Bilgiler'de yazılanı tekrar etme. Düz cümle, aktarım fiili yok. Diğerleriyle aynıysa "aynı çizgide" yeterli.
- **@diğer (Ad):** ...
Kaynak: <link> <link>

---

🐦 HESAPLARA ÖZEL

### @hesap (Ad)

**[Konu Başlığı]**
Özü ve en önemli bir-iki detay, kısa tek paragraf. Kaynak: <link>

**[Konu 2 Başlığı]**
Aynı şekilde. Kaynak: <link>

### @diğer (Ad)
...

Kısıtlar:
- Kısa ve öz, haber bülteni dili.
- Doğrudan "📌 ORTAK GÜNDEM" ile başla; giriş cümlesi yok. ORTAK GÜNDEM'e girecek konu yoksa bölümü "📌 ORTAK GÜNDEM\n(Birden fazla hesabın ortak işlediği konu yok.)" olarak yaz.
</görev>"""

FINAL_MERGE_PROMPT = """<grup_raporları>
{batch_reports}
</grup_raporları>

<görev>
Yukarıda aynı dönemin X (Twitter) paylaşımlarından, farklı gruplar halinde üretilmiş gündem raporları var. Bunları tek bir nihai rapora birleştir.

Kurallar:
1. DIŞ BİLGİ YOK: sadece girdideki bilgi. 2. TEKRAR YOK: aynı olay iki grupta geçiyorsa tek konuda birleştir, bilgiyi bir kez yaz. 3. Aynı somut olayı birden fazla hesap paylaşmışsa "📌 ORTAK GÜNDEM", tek hesapsa "🐦 HESAPLARA ÖZEL". Aynı kişi adı yetmez, olay aynı olmalı. 4. ORTAK GÜNDEM'i en çok hesabın paylaştığı konu üstte olacak şekilde sırala. 5. HESAPLARA ÖZEL'de aynı hesap tek "### @hesap" başlığı altında. 6. Kaynak linkleri koru; her konuda en fazla 3 link, < > ile sarılı; link uydurma. 7. Metafor, süs sıfatı ve pasif aktarım fiili yasak; düz haber dili. 8. Başlık: somut aktör + eylem + çarpıcı unsur; clickbait yok.

Şablon:

📌 ORTAK GÜNDEM

### [Spesifik Konu Başlığı]
**Ortak Bilgiler:** ...
**Hesap Katkıları:**
- **@hesap (Ad):** ...
Kaynak: <link>

---

🐦 HESAPLARA ÖZEL

### @hesap (Ad)

**[Konu Başlığı]**
... Kaynak: <link>

Doğrudan "📌 ORTAK GÜNDEM" ile başla; giriş cümlesi yok.
</görev>"""


# ---------------------------------------------------------------------------
# Gemini (Youtube pipeline ile aynı key/model rotasyonu)
# ---------------------------------------------------------------------------

def _gemini_error_kind(e):
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
    """Returns text or None. Key/model rotation identical to main.py."""
    from google import genai
    from google.genai import types

    log(f"🤖 [{label}] Gemini çağrısı (prompt: {len(prompt):,} karakter)", indent=1)
    config_kwargs = {"thinking_config": types.ThinkingConfig(thinking_level=types.ThinkingLevel.HIGH)}
    if response_schema is not None:
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = response_schema

    for m_pos, model in enumerate(MODEL_CHAIN):
        keys = _live_keys(model)
        if not keys:
            if m_pos + 1 < len(MODEL_CHAIN):
                continue
            break
        log(f"🧠 Model: {model} ({len(keys)} kullanılabilir key)", indent=2)
        for key_idx, key in keys:
            client = genai.Client(api_key=key)
            for attempt in range(3):
                t0 = time.time()
                try:
                    response = client.models.generate_content(
                        model=model, contents=prompt,
                        config=types.GenerateContentConfig(**config_kwargs))
                    elapsed = time.time() - t0
                    if response.text:
                        log(f"✅ [{label}] Yanıt alındı ({model}, key #{key_idx}, {elapsed:.1f}s, {len(response.text):,} karakter)", indent=2)
                        return response.text.strip()
                    log(f"⚠️ [{label}] Boş yanıt ({elapsed:.1f}s)", indent=2)
                except Exception as e:
                    elapsed = time.time() - t0
                    err = str(e)
                    kind = _gemini_error_kind(e)
                    if kind == "dead":
                        _dead_keys[key_idx] = err[:120]
                        log(f"⛔ Key #{key_idx} kullanılamıyor: {err[:160]}", indent=2)
                        break
                    if kind == "daily":
                        _quota_dead.add((model, key_idx))
                        log(f"⛔ Key #{key_idx} günlük kotası '{model}' için dolu", indent=2)
                        break
                    # rate/transient (503 yoğunluk, bağlantı kopması) ve diğer hatalar: aynı key'de kısa
                    # beklemeyle yeniden dene; 3 deneme sonunda sıradaki key'e geç.
                    wait = (attempt + 1) * 15
                    log(f"🔁 Key #{key_idx} {kind} ({elapsed:.1f}s, deneme {attempt+1}/3): {err[:160]}"
                        + (f" — {wait}s bekleniyor" if attempt < 2 else " — sıradaki key"), indent=2)
                    if attempt < 2:
                        time.sleep(wait)
        # Bu modelde her key tükendi (kota, geçersiz veya tekrarlayan 503). 503 "high demand" modele
        # özgü olduğundan sıradaki modele geçmek genellikle çözer.
        if m_pos + 1 < len(MODEL_CHAIN):
            reason = "kotası doldu" if not _live_keys(model) else "tüm key'lerde başarısız"
            log(f"➡️ '{model}' {reason}, '{MODEL_CHAIN[m_pos + 1]}' modeline geçiliyor", indent=1)
            continue
        break
    log(f"❌ [{label}] Tüm modeller, key'ler ve denemeler tükendi", indent=1)
    return None


# ---------------------------------------------------------------------------
# Apify
# ---------------------------------------------------------------------------

APIFY_BASE = "https://api.apify.com/v2"


def apify_run(actor, run_input, label, timeout_s=1500, poll_s=15):
    """Start an actor run, wait for it, return the dataset items (list). Raises on failure."""
    log(f"🕷️ Apify [{label}] başlatılıyor: {actor}", indent=1)
    r = requests.post(f"{APIFY_BASE}/acts/{actor}/runs",
                      params={"token": APIFY_API_KEY, "timeout": timeout_s},
                      json=run_input, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Apify run başlatılamadı ({r.status_code}): {r.text[:300]}")
    run = r.json()["data"]
    run_id, dataset_id = run["id"], run["defaultDatasetId"]
    t0 = time.time()
    status = run.get("status")
    while status not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
        if time.time() - t0 > timeout_s + 120:
            raise RuntimeError(f"Apify run {run_id} zaman aşımı")
        time.sleep(poll_s)
        rr = requests.get(f"{APIFY_BASE}/actor-runs/{run_id}", params={"token": APIFY_API_KEY}, timeout=60)
        rr.raise_for_status()
        status = rr.json()["data"].get("status")
    log(f"🕷️ Apify [{label}] durum: {status} ({time.time() - t0:.0f}s)", indent=1)
    if status != "SUCCEEDED":
        raise RuntimeError(f"Apify run {run_id} {status}")

    items, offset, limit = [], 0, 1000
    while True:
        d = requests.get(f"{APIFY_BASE}/datasets/{dataset_id}/items",
                         params={"token": APIFY_API_KEY, "clean": "true", "offset": offset, "limit": limit},
                         timeout=120)
        d.raise_for_status()
        page = d.json()
        items.extend(page)
        if len(page) < limit:
            break
        offset += limit
    log(f"🕷️ Apify [{label}] {len(items)} kayıt alındı", indent=1)
    return items


def _first(d, *keys, default=None):
    """Return the first non-empty value among (possibly nested, dot-separated) keys."""
    for k in keys:
        cur = d
        ok = True
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur and cur[part] not in (None, ""):
                cur = cur[part]
            else:
                ok = False
                break
        if ok:
            return cur
    return default


def _handle_of(item):
    h = _first(item, "userName", "username", "screen_name", "screenName", "handle",
               "author.userName", "author.username", "author.screen_name", "user.screen_name",
               "user.userName", "user.username", "legacy.screen_name")
    if not h and isinstance(_first(item, "url", "twitterUrl"), str):
        m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})", _first(item, "url", "twitterUrl"))
        if m:
            h = m.group(1)
    return (h or "").lstrip("@").strip()


# ---------------------------------------------------------------------------
# Hesap listesi
# ---------------------------------------------------------------------------

def _read_accounts_file():
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    out = []
    with open(ACCOUNTS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip().lstrip("@")
            if line:
                out.append(line)
    return out


def _load_followings_cache():
    if not os.path.exists(FOLLOWINGS_FILE):
        return None
    try:
        with open(FOLLOWINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"⚠️ followings.json okunamadı: {e}")
        return None


def build_following_input(spec, handle, max_items):
    kind = spec.get("kind")
    if kind == "user_scraper":
        inp = {"twitterHandles": [handle], "getFollowing": True, "getFollowers": False, "maxItems": max_items}
    elif kind == "userId":
        inp = {"userId": handle}
    elif kind == "urls":
        inp = {"urls": [f"https://x.com/{handle}/following"], "maxResults": max_items}
    elif kind == "handles":
        inp = {"twitterHandles": [handle], "maxItems": max_items}
    else:
        raise ValueError(f"bilinmeyen following kind: {kind}")
    inp.update(spec.get("input") or {})
    return inp


def run_chain(chain, build_input, label, expected, timeout_s=900):
    """Zinciri sırayla dener. Returns (items, actor_id or None, notes).
    Ücretsiz aktör: aylık hakkı kalmadıysa atlanır; sonuç cap'e takıldıysa eksik sayılır ve
    sıradakine geçilir (eksik sonuç yine de yedek olarak saklanır). Ücretli aktör: aylık kayıt
    bütçesi bittiyse atlanır; başarılı olunca kayıt sayısı bütçeden düşülür."""
    notes, fallback = [], None
    for spec in chain:
        actor = spec["actor"]
        is_free = bool(spec.get("free"))
        if is_free and _free_runs_left(spec) <= 0:
            notes.append(f"{actor}: ücretsiz aylık koşu hakkı bitti")
            continue
        if not is_free and _paid_budget_left() <= 0:
            notes.append(f"{actor}: aylık ücretli bütçe dolu ({STATE['paid_items']}/{MONTHLY_PAID_ITEMS_BUDGET})")
            continue
        max_items = expected if is_free else max(1, min(expected, _paid_budget_left()))
        try:
            run_input = build_input(spec, max_items)
            items = apify_run(actor, run_input, f"{label} · {actor}", timeout_s=timeout_s)
        except Exception as e:
            notes.append(f"{actor}: hata: {str(e)[:160]}")
            log(f"❌ {actor} başarısız: {str(e)[:200]}", indent=1)
            if is_free:
                STATE["free_runs"][actor] = STATE["free_runs"].get(actor, 0) + 1
            continue
        if is_free:
            STATE["free_runs"][actor] = STATE["free_runs"].get(actor, 0) + 1
            cap = spec.get("cap")
            if cap and len(items) >= cap and expected > cap:
                notes.append(f"{actor}: ücretsiz sonuç {cap} kayıtta kesildi, ücretli aktöre geçiliyor")
                log(f"⚠️ {actor}: {len(items)} kayıt = ücretsiz tavan, eksik sayılıyor", indent=1)
                fallback = (items, actor)
                continue
        else:
            STATE["paid_items"] += len(items)
            STATE["paid_runs"] += 1
        return items, actor, notes
    if fallback:
        notes.append(f"{FALLBACK_NOTE} ({fallback[1]})")
        return fallback[0], fallback[1], notes
    return [], None, notes


def fetch_followings(handle):
    """Fetch the public following list of `handle`. Returns ([{handle, name}], actor, notes)."""
    chain = load_actors()["followings"]
    items, actor, notes = run_chain(
        chain, lambda spec, n: build_following_input(spec, handle, n),
        f"takip listesi @{handle}", expected=2000)
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        h = _handle_of(it)
        if not h or h.lower() == handle.lower() or h.lower() in seen:
            continue
        seen.add(h.lower())
        out.append({"handle": h, "name": _first(it, "name", "author.name", "user.name", "legacy.name", default=h)})
    return out, actor, notes


def get_accounts():
    """Returns [{handle, name}] and a short description of the source for the report header."""
    manual = _read_accounts_file()
    if manual:
        log(f"📋 Hesap listesi accounts.txt'den alındı ({len(manual)} hesap)")
        return [{"handle": h, "name": h} for h in manual], "accounts.txt"

    cache = _load_followings_cache()
    fresh = False
    if cache and not REFRESH_FOLLOWINGS:
        try:
            fetched = datetime.fromisoformat(cache["fetched_at"])
            fresh = (datetime.now(timezone.utc) - fetched) < timedelta(days=FOLLOWINGS_REFRESH_DAYS)
        except Exception:
            fresh = False
        if cache.get("handle", "").lower() != X_HANDLE.lower():
            fresh = False
    if cache and not cache.get("complete"):
        fresh = False  # ücretsiz aktörün 10'luk tavanına takılmış veya eski biçimli liste: yenile
    if cache and fresh and cache.get("accounts"):
        log(f"📋 Takip listesi önbellekten alındı ({len(cache['accounts'])} hesap, {cache['fetched_at'][:10]})")
        return cache["accounts"], f"@{X_HANDLE} takip listesi (önbellek)"

    if not X_HANDLE:
        raise SystemExit("X_HANDLE tanımlı değil ve twitter/accounts.txt boş: takip edilecek hesap yok.")
    try:
        accounts, actor, notes = fetch_followings(X_HANDLE)
        for n in notes:
            log(f"ℹ️ {n}", indent=1)
        if not accounts:
            raise RuntimeError("takip listesi boş döndü")
        # Ücretsiz aktörün 10'luk tavanına takılmış eksik liste, daha uzun bir önbellekten daha iyi değil:
        # elimizde daha büyük bir eski liste varsa onu koru.
        incomplete = any(n.startswith(FALLBACK_NOTE) for n in notes)
        if cache and cache.get("accounts") and len(cache["accounts"]) > len(accounts) and incomplete:
            log(f"↩️ Yeni liste ({len(accounts)}) eski önbellekten ({len(cache['accounts'])}) küçük ve kesik; eski liste korunuyor")
            return cache["accounts"], f"@{X_HANDLE} takip listesi (önbellek)"
        complete = not incomplete
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(FOLLOWINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"handle": X_HANDLE, "fetched_at": datetime.now(timezone.utc).isoformat(),
                       "actor": actor, "complete": complete, "accounts": accounts}, f, ensure_ascii=False, indent=1)
        log(f"📋 Takip listesi alındı ve önbelleğe yazıldı ({len(accounts)} hesap, {actor})")
        return accounts, f"@{X_HANDLE} takip listesi"
    except Exception as e:
        log(f"❌ Takip listesi çekilemedi: {e}")
        if cache and cache.get("accounts"):
            log(f"↩️ Eski önbellek kullanılıyor ({len(cache['accounts'])} hesap, {cache.get('fetched_at', '?')[:10]})")
            return cache["accounts"], f"@{X_HANDLE} takip listesi (eski önbellek)"
        raise SystemExit("Hesap listesi yok, çıkılıyor.")


# ---------------------------------------------------------------------------
# Tweet çekme ve normalizasyon
# ---------------------------------------------------------------------------

def _parse_dt(value):
    """Apify/Twitter tarih biçimlerini aware datetime'a çevirir; başarısızsa None."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(value).strip()
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def normalize_tweet(item):
    """Apify kaydını sabit şemaya indirger. Tanınmazsa None."""
    tid = _first(item, "id", "id_str", "tweetId", "rest_id")
    text = _first(item, "fullText", "full_text", "text", "legacy.full_text", default="")
    if not tid or not isinstance(text, str):
        return None
    handle = _handle_of(item)
    name = _first(item, "author.name", "user.name", "name", "legacy.name", default=handle)
    url = _first(item, "url", "twitterUrl", default=f"https://x.com/{handle}/status/{tid}")
    created = _parse_dt(_first(item, "createdAt", "created_at", "timestamp", "legacy.created_at"))
    is_reply = bool(_first(item, "isReply", "is_reply", default=False)) or bool(_first(item, "inReplyToId", "in_reply_to_status_id_str"))
    is_rt = bool(_first(item, "isRetweet", "is_retweet", default=False)) or text.startswith("RT @")
    rt = _first(item, "retweetedTweet", "retweeted_status")
    if isinstance(rt, dict):
        rt_text = _first(rt, "fullText", "full_text", "text", default="")
        rt_handle = _handle_of(rt)
        if rt_text:
            text = f"RT @{rt_handle}: {rt_text}" if rt_handle else f"RT: {rt_text}"
        is_rt = True
    quote = _first(item, "quote", "quotedTweet", "quoted_status")
    if isinstance(quote, dict):
        q_text = _first(quote, "fullText", "full_text", "text", default="")
        q_handle = _handle_of(quote)
        if q_text:
            text = f"{text}\n↪ Alıntı @{q_handle}: {q_text}" if q_handle else f"{text}\n↪ Alıntı: {q_text}"
    text = re.sub(r"\s+", " ", text).strip()
    return {
        "id": str(tid), "handle": handle, "name": name or handle, "text": text, "url": url,
        "created_at": created.isoformat() if created else None,
        "likes": int(_first(item, "likeCount", "favorite_count", "favoriteCount", "legacy.favorite_count", default=0) or 0),
        "retweets": int(_first(item, "retweetCount", "retweet_count", "legacy.retweet_count", default=0) or 0),
        "replies": int(_first(item, "replyCount", "reply_count", default=0) or 0),
        "views": int(_first(item, "viewCount", "views", default=0) or 0),
        "is_reply": is_reply, "is_retweet": is_rt,
    }


def _x_ts(dt):
    """X arama operatörü zaman biçimi: since:2026-10-11_12:00:00_UTC"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d_%H:%M:%S_UTC")


def build_tweet_input(spec, handles, since, until, max_items):
    kind = spec.get("kind")
    if kind == "handles":
        inp = {
            "twitterHandles": handles, "maxItems": max_items, "sort": "Latest",
            # Tarih filtresi gün hassasiyetinde; kesin pencere select_tweets'te uygulanır.
            "start": (since - timedelta(days=1)).strftime("%Y-%m-%d"),
            "end": (until + timedelta(days=1)).strftime("%Y-%m-%d"),
        }
    elif kind == "search":
        replies = "" if INCLUDE_REPLIES else " -filter:replies"
        terms = [f"from:{h} since:{_x_ts(since)} until:{_x_ts(until)}{replies}" for h in handles]
        inp = {"searchTerms": terms, "maxItems": max_items, "sort": "Latest"}
    else:
        raise ValueError(f"bilinmeyen tweet kind: {kind}")
    inp.update(spec.get("input") or {})
    return inp


def fetch_tweets(accounts, since, until):
    """Aktör zinciriyle hesapların pencere içindeki paylaşımlarını çeker. Returns (items, notes)."""
    handles = [a["handle"] for a in accounts]
    chain = load_actors()["tweets"]
    all_items, all_notes, used = [], [], set()
    chunks = [handles[i:i + HANDLES_PER_RUN] for i in range(0, len(handles), HANDLES_PER_RUN)]
    log(f"🐦 {len(handles)} hesap, {len(chunks)} koşu ({HANDLES_PER_RUN} hesap/koşu)")
    for n, chunk in enumerate(chunks, 1):
        expected = min(MAX_TWEETS_PER_ACCOUNT * len(chunk), MAX_TWEETS_TOTAL)
        items, actor, notes = run_chain(
            chain, lambda spec, m: build_tweet_input(spec, chunk, since, until, m),
            f"tweet {n}/{len(chunks)}", expected=expected, timeout_s=1500)
        for note in notes:
            log(f"ℹ️ {note}", indent=1)
        all_notes.extend(notes)
        if actor:
            used.add(actor)
        all_items.extend(items)
    if used:
        all_notes.append("kullanılan aktör: " + ", ".join(sorted(used)))
    return all_items, all_notes


# ---------------------------------------------------------------------------
# PROBE modu: her aday aktörü küçük bir girdiyle çalıştırır, ham çıktı örneklerini twitter/probe/ altına
# yazar. Aktörlerin gerçek girdi/çıktı şemasını görmek için; Gemini ve Discord kullanılmaz.
# ---------------------------------------------------------------------------

def probe():
    actors = load_actors()
    os.makedirs(PROBE_DIR, exist_ok=True)
    since, until = window_bounds()
    sample_handle = (_read_accounts_file() or ["GazeteOksijen"])[0]
    cache = _load_followings_cache()
    if cache and cache.get("accounts"):
        sample_handle = cache["accounts"][0]["handle"]
    jobs = []
    for spec in actors["followings"]:
        if not X_HANDLE:
            continue
        jobs.append(("followings", spec, lambda s=spec: build_following_input(s, X_HANDLE, 100)))
    for spec in actors["tweets"]:
        jobs.append(("tweets", spec, lambda s=spec: build_tweet_input(s, [sample_handle], since - timedelta(days=6), until, 5)))
    summary = []
    for group, spec, builder in jobs:
        actor = spec["actor"]
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", actor)
        rec = {"group": group, "actor": actor, "kind": spec.get("kind"), "free": bool(spec.get("free")),
               "probed_at": datetime.now(timezone.utc).isoformat()}
        try:
            rec["input"] = builder()
            items = apify_run(actor, rec["input"], f"PROBE {group}", timeout_s=600)
            rec["count"] = len(items)
            rec["sample"] = items[:3]
            rec["handles_seen"] = sorted({_handle_of(i) for i in items if isinstance(i, dict)})[:20]
            if group == "tweets":
                norm = [normalize_tweet(i) for i in items if isinstance(i, dict)]
                rec["normalized_ok"] = sum(1 for t in norm if t)
                rec["normalized_sample"] = [t for t in norm if t][:2]
            if spec.get("free"):
                STATE["free_runs"][actor] = STATE["free_runs"].get(actor, 0) + 1
            else:
                STATE["paid_items"] += len(items); STATE["paid_runs"] += 1
            summary.append(f"✅ {group} {actor}: {len(items)} kayıt")
        except Exception as e:
            rec["error"] = str(e)[:1000]
            summary.append(f"❌ {group} {actor}: {str(e)[:120]}")
        with open(os.path.join(PROBE_DIR, f"{group}-{safe}.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1, default=str)
    with open(os.path.join(PROBE_DIR, "summary.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(summary) + "\n")
    for s in summary:
        log(s)


def select_tweets(raw_items, since, until, allowed_handles=None):
    """Normalize, pencereye göre süz, yanıtları (ayar) ve tekrarları at."""
    allowed = {h.lower() for h in allowed_handles} if allowed_handles else None
    out, seen_ids, seen_text = [], set(), set()
    stats = {"raw": len(raw_items), "unparsed": 0, "out_of_window": 0, "reply": 0, "dup": 0, "foreign": 0, "short": 0}
    for it in raw_items:
        t = normalize_tweet(it) if isinstance(it, dict) else None
        if not t:
            stats["unparsed"] += 1
            continue
        if allowed and t["handle"].lower() not in allowed:
            stats["foreign"] += 1
            continue
        if t["created_at"]:
            dt = datetime.fromisoformat(t["created_at"])
            if dt < since or dt > until:
                stats["out_of_window"] += 1
                continue
        if t["is_reply"] and not INCLUDE_REPLIES:
            stats["reply"] += 1
            continue
        if t["id"] in seen_ids:
            stats["dup"] += 1
            continue
        key = re.sub(r"https?://\S+", "", t["text"]).lower().strip()
        if len(key) < 15:
            stats["short"] += 1
            continue
        if key in seen_text:
            stats["dup"] += 1
            continue
        seen_ids.add(t["id"])
        seen_text.add(key)
        out.append(t)
    out.sort(key=lambda t: (t["created_at"] or ""), reverse=True)
    log(f"🧹 Süzme: {stats['raw']} ham → {len(out)} paylaşım "
        f"(pencere dışı {stats['out_of_window']}, yanıt {stats['reply']}, tekrar {stats['dup']}, "
        f"kısa {stats['short']}, tanınmayan {stats['unparsed']}, liste dışı {stats['foreign']})")
    return out


# ---------------------------------------------------------------------------
# Gemini aşamaları
# ---------------------------------------------------------------------------

def _tweet_line(i, t, with_topic=None):
    text = t["text"].replace("\n", " ")
    if len(text) > 900:
        text = text[:900] + "…"
    mid = f" | {with_topic}" if with_topic else ""
    return f"[{i}] @{t['handle']} ({t['name']}){mid} | ❤️ {t['likes']} 🔁 {t['retweets']} | {text} | {t['url']}"


TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "topic": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["index", "topic", "summary"],
            },
        },
    },
    "required": ["items"],
}


def triage(tweets):
    """ELEME: haber değeri taşıyanları seçer. Returns list of {tweet, topic, summary}.
    Bir batch'in Gemini çağrısı başarısız olursa o batch'teki her şey tutulur (iş kaybolmasın)."""
    kept = []
    batches = [tweets[i:i + TRIAGE_BATCH] for i in range(0, len(tweets), TRIAGE_BATCH)]
    log(f"🔍 ELEME: {len(tweets)} paylaşım, {len(batches)} batch")
    for n, batch in enumerate(batches, 1):
        payload = "\n".join(_tweet_line(i, t) for i, t in enumerate(batch))
        result = call_gemini(TRIAGE_PROMPT.format(payload=payload), label=f"ELEME {n}/{len(batches)}",
                             response_schema=TRIAGE_SCHEMA)
        picked = None
        if result:
            try:
                data = json.loads(result)
                picked = [it for it in data.get("items", [])
                          if isinstance(it.get("index"), int) and 0 <= it["index"] < len(batch)]
            except Exception as e:
                log(f"⚠️ ELEME {n} JSON çözümlenemedi: {e}", indent=1)
        if picked is None:
            log(f"⚠️ ELEME {n} başarısız, batch'in tamamı tutuluyor", indent=1)
            kept.extend({"tweet": t, "topic": "", "summary": t["text"][:300]} for t in batch)
            continue
        seen = set()
        for it in picked:
            if it["index"] in seen:
                continue
            seen.add(it["index"])
            kept.append({"tweet": batch[it["index"]], "topic": (it.get("topic") or "").strip(),
                         "summary": (it.get("summary") or "").strip() or batch[it["index"]]["text"][:300]})
        log(f"✅ ELEME {n}: {len(seen)}/{len(batch)} tutuldu", indent=1)
    return kept


def _news_line(i, k):
    t = k["tweet"]
    summary = k["summary"].replace("\n", " ")
    return f"[{i}] @{t['handle']} ({t['name']}) | {k['topic'] or '-'} | ❤️ {t['likes']} 🔁 {t['retweets']} | {summary} | {t['url']}"


def build_report(news, window_label):
    """RAPOR: haberleri konu bazında birleştirir. Çok fazlaysa batch + final merge."""
    # Aynı konu etiketleri yan yana gelsin ki batch'ler konuyu bölmesin.
    news = sorted(news, key=lambda k: (k["topic"].lower(), -(k["tweet"]["likes"] + k["tweet"]["retweets"])))
    if len(news) <= REPORT_BATCH:
        payload = "\n".join(_news_line(i, k) for i, k in enumerate(news))
        log(f"📝 RAPOR: {len(news)} haber, tek çağrı")
        out = call_gemini(REPORT_PROMPT.format(payload=payload, window_label=window_label), label="RAPOR")
        return out

    batches = [news[i:i + REPORT_BATCH] for i in range(0, len(news), REPORT_BATCH)]
    log(f"📝 RAPOR: {len(news)} haber, {len(batches)} batch + final birleştirme")
    outputs = []
    for n, b in enumerate(batches, 1):
        payload = "\n".join(_news_line(i, k) for i, k in enumerate(b))
        out = call_gemini(REPORT_PROMPT.format(payload=payload, window_label=window_label),
                          label=f"RAPOR {n}/{len(batches)}")
        if out:
            outputs.append(out)
        else:
            log(f"⚠️ RAPOR {n} başarısız, ham özetler kullanılacak", indent=1)
            outputs.append("\n".join(f"- @{k['tweet']['handle']}: {k['summary']} <{k['tweet']['url']}>" for k in b))
    merge_input = "\n\n=== GRUP AYIRICI ===\n\n".join(f"[GRUP {i+1}]\n\n{o}" for i, o in enumerate(outputs))
    final = call_gemini(FINAL_MERGE_PROMPT.format(batch_reports=merge_input), label="FİNAL BİRLEŞTİRME")
    return final or "\n\n---\n\n".join(outputs)


# ---------------------------------------------------------------------------
# Çıktı
# ---------------------------------------------------------------------------

def send_to_discord(report):
    log(f"📤 Discord'a gönderiliyor ({len(report):,} karakter)...")
    sent, first = 0, True
    while report:
        if len(report) <= 1900:
            chunk, report = report, ""
        else:
            split_at = report.rfind("\n", 0, 1900)
            if split_at == -1:
                split_at = report.rfind(" ", 0, 1900)
            if split_at == -1:
                split_at = 1900
            chunk, report = report[:split_at], report[split_at:].lstrip()
        payload = {"content": chunk, "allowed_mentions": {"parse": []}}
        if first and DISCORD_MENTION_USER_ID:
            payload["content"] = f"<@{DISCORD_MENTION_USER_ID}>\n{chunk}"
            payload["allowed_mentions"] = {"parse": [], "users": [DISCORD_MENTION_USER_ID]}
        first = False
        for attempt in range(1, 4):
            try:
                r = requests.post(DISCORD_URL, json=payload, timeout=30)
                if r.status_code == 429 and attempt < 3:
                    try:
                        retry = r.json().get("retry_after", 5)
                    except Exception:
                        retry = 5
                    time.sleep(float(retry) + 0.5)
                    continue
                if r.status_code >= 400:
                    log(f"⚠️ Discord {r.status_code}: {r.text[:200]}", indent=1)
                sent += 1
                break
            except Exception as e:
                log(f"❌ Discord gönderim hatası: {e}", indent=1)
                break
        time.sleep(0.5)
    log(f"✅ Discord gönderimi tamamlandı ({sent} parça)")


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    log(f"💾 Yazıldı: {os.path.relpath(path, BASE_DIR)}")


def window_bounds(now=None):
    until = now or datetime.now(timezone.utc)
    since = until - timedelta(hours=WINDOW_HOURS)
    return since, until


def window_label(since, until):
    s, u = since.astimezone(ISTANBUL_TZ), until.astimezone(ISTANBUL_TZ)
    if WINDOW_HOURS <= 36:
        return f"son {WINDOW_HOURS} saat ({s.strftime('%d.%m %H:%M')} – {u.strftime('%d.%m %H:%M')})"
    return f"{s.strftime('%d.%m.%Y')} – {u.strftime('%d.%m.%Y')} arası ({WINDOW_HOURS // 24} gün)"


def main():
    global STATE
    t_start = time.time()
    log(f"🚀 TWITTER HABER AJANSI başlıyor" + (f" (mod: {PIPELINE_MODE})" if PIPELINE_MODE != "full" else ""))
    if PIPELINE_MODE not in ("full", "scrape", "probe"):
        raise SystemExit(f"Bilinmeyen PIPELINE_MODE '{PIPELINE_MODE}' (full, scrape veya probe)")
    if not APIFY_API_KEY:
        raise SystemExit("APIFY_API_KEY tanımlı değil")
    if PIPELINE_MODE == "full" and not GEMINI_API_KEYS:
        raise SystemExit("GEMINI_API_KEY_1 tanımlı değil")
    if PIPELINE_MODE == "full" and not DISCORD_URL:
        raise SystemExit("DISCORD_WEBHOOK_URL tanımlı değil")

    STATE = load_state()
    log(f"📊 Ay {STATE['month']}: ücretsiz koşular {STATE['free_runs'] or '{}'}, "
        f"ücretli kayıt {STATE['paid_items']}/{MONTHLY_PAID_ITEMS_BUDGET}")
    try:
        _main_body(t_start)
    finally:
        save_state(STATE)


def _main_body(t_start):
    if PIPELINE_MODE == "probe":
        probe()
        log(f"🏁 Probe tamamlandı ({time.time() - t_start:.0f}s)")
        return

    since, until = window_bounds()
    label = window_label(since, until)
    log(f"🗓️ Pencere: {label}")

    accounts, source_desc = get_accounts()
    raw, fetch_notes = fetch_tweets(accounts, since, until)
    tweets = select_tweets(raw, since, until, allowed_handles=[a["handle"] for a in accounts])
    day = until.astimezone(ISTANBUL_TZ).strftime("%Y-%m-%d")
    save_json(TWEETS_FILE, {"window": {"since": since.isoformat(), "until": until.isoformat(), "hours": WINDOW_HOURS},
                            "source": source_desc, "accounts": len(accounts), "notes": fetch_notes, "tweets": tweets})
    if PIPELINE_MODE == "scrape":
        log(f"🏁 Scrape tamamlandı ({len(tweets)} paylaşım, {time.time() - t_start:.0f}s)")
        return

    warn = [n for n in fetch_notes if "bütçe" in n or "hata" in n or n.startswith(FALLBACK_NOTE)]
    warn_line = f"⚠️ {' · '.join(warn)}\n" if warn else ""
    if not tweets:
        msg = f"📅 **{day}** · Twitter Gündem\nPencere: {label}\n{len(accounts)} hesap tarandı, paylaşım bulunamadı.\n{warn_line}"
        send_to_discord(msg)
        return

    news = triage(tweets)
    log(f"🔍 ELEME sonucu: {len(news)}/{len(tweets)} haber değeri taşıyor")
    if not news:
        msg = (f"📅 **{day}** · Twitter Gündem\nPencere: {label}\n"
               f"{len(accounts)} hesap, {len(tweets)} paylaşım tarandı; haber değeri taşıyan paylaşım bulunamadı.\n{warn_line}")
        send_to_discord(msg)
        return

    report = build_report(news, label)
    if not report:
        log("❌ Rapor üretilemedi, ham özetler gönderiliyor")
        report = "\n".join(f"- @{k['tweet']['handle']}: {k['summary']} <{k['tweet']['url']}>" for k in news)

    active = len({k["tweet"]["handle"].lower() for k in news})
    header = (f"📅 **{day}** · 🐦 **Twitter Gündem Raporu**\n"
              f"Pencere: {label}\n"
              f"Kaynak: {source_desc} · {len(accounts)} hesap · {len(tweets)} paylaşım tarandı · "
              f"{len(news)} haber ({active} hesaptan) raporlandı, {len(tweets) - len(news)} gürültü elendi\n"
              f"{warn_line}\n---\n\n")
    full = header + report

    os.makedirs(REPORTS_DIR, exist_ok=True)
    suffix = "" if WINDOW_HOURS <= 36 else f"-{WINDOW_HOURS // 24}gun"
    path = os.path.join(REPORTS_DIR, f"{day}{suffix}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(full)
    log(f"💾 Yazıldı: {os.path.relpath(path, BASE_DIR)}")

    send_to_discord(full)
    log(f"🏁 Tamamlandı ({time.time() - t_start:.0f}s)")


if __name__ == "__main__":
    main()
