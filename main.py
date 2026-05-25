import json
import math
import os
import re
import time
import requests
import isodate
from datetime import datetime, timedelta, timezone
from google import genai
from google.genai import types

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL")
APIFY_API_KEY = os.getenv("APIFY_API_KEY")
GEMINI_API_KEYS = [
    os.getenv("GEMINI_API_KEY_1"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
]

CHANNELS = {
    "Serdar Akinan": "@serdarakinan",
    "Erdem Atay": "@erdemataytv",
    "Veryansın Tv": "@VeryansinTv",
    "Onlar TV": "@onlartv",
    "Yılmaz Özdil": "@yilmaz_ozdil",
    "Cem Gürdeniz": "@cemgurdenizz",
    "Caspian Report": "@CaspianReport",
    "Fatih Altaylı": "@fatihaltayli",
}

PROMPT_TEMPLATE = """<transkript>
{transkript}
</transkript>

<görev>
Yukarıdaki transkript, {channel_name} kanalında yayınlanan "{video_title}" başlıklı videonun transkriptidir.
Amacın, bu videoyu izlemeyen birine, transkriptteki BÜTÜN bilgileri (sayılar, isimler, iddialar) hiçbir eksik olmadan, mantıklı bir olay örgüsü içinde ve yüksek okunabilirlikle aktarmaktır.
Rapora asla kendi yorumunu ekleme.

Bunu başarmak için, bir insanın yapacağı gibi şu iki aşamalı yöntemi KESİNLİKLE uygulamak zorundasın:

### AŞAMA 1: KONULARA AYIRMA VE İÇİNİ DOLDURMA (Veri Kaybını Önleme)
1. Transkripti baştan sona tara. Konuşulan farklı olayları/gündem maddelerini tespit et.
2. Tespit ettiğin her bir konu için bir başlık aç.
3. O konuyla ilgili videoda geçen HER TÜRLÜ spesifik detayı, sayıyı, ismi, iddiayı ve havada kalan benzetmeleri (örn: "ölüm öpücüğü") o başlığın altına ham notlar/maddeler halinde doldur.
4. Bu aşamada akıcı olmana gerek yok, amacın "bölümleri oluşturmak ve içlerini transkriptteki verilerle tıka basa doldurmaktır".
5. Bu ham taslağı KESİNLİKLE <taslak> ... </taslak> etiketleri arasına yaz.

### AŞAMA 2: NİHAİ RAPOR (Mantık Sırası ve Okunabilirlik)
1. Şimdi transkripti unut ve sadece <taslak> bölümünde kendi oluşturduğun o bölümlere ve içindeki verilere odaklan.
2. Taslaktaki her bir bölümü, akıcı, profesyonel ve mantık sırasına oturtulmuş bir olay örgüsüne dönüştür. (Örn: Önce iddiayı, sonra sebebini anlat).
3. Taslağına koyduğun hiçbir ismi, sayıyı veya benzetmeyi dışarıda bırakma; hepsini tam ve anlaşılır cümlelerle bu akıcı metne yedir.
4. Bu nihai raporu <rapor> ... </rapor> etiketleri arasına yaz.

ÇIKTI FORMATI:
Sadece ve doğrudan <taslak> etiketiyle başla, ardından <rapor> etiketini oluştur. Başka hiçbir giriş cümlesi kullanma.
</görev>"""

CONSOLIDATION_PROMPT = """<raporlar>
{combined_reports}
</raporlar>

<görev>
Yukarıda farklı kanallara ait video raporları bulunmaktadır. Bu raporları TEK bir konsolide rapor haline getireceksin. 
Rapora asla kendi yorumunu ekleme.

YAPI VE KURALLAR:
Tüm raporları oku. Aynı SOMUT OLAYI (örneğin sadece 'Erdoğan' kelimesi geçmesini değil, 'Erdoğan'ın X açıklaması' gibi spesifik bir olayı) birden fazla kanal işlemişse "ORTAK GÜNDEM"e, sadece tek bir kanal işlemişse "KANALLARA ÖZEL HABERLER"e ekle. Birbiriyle ilgisiz konuları zorlama başlıklar altında birleştirme.

Aşağıdaki şablonu BİREBİR kullan:

📌 ORTAK GÜNDEM

### [Spesifik Konu Başlığı]
**Olayın Arka Planı:** (Bu konunun neden gündem olduğunu, meselenin özünü ve tarafların genel durumunu anlatan, okuyucuyu aydınlatacak en az 3-4 cümlelik doyurucu ve akıcı bir paragraf yaz. Asla tek cümleyle geçiştirme.)
**Kanal Katkıları:**
- **[Kanal Adı] / [Yorumcu]:** Orijinal rapordaki maddeleri, bağlamını ve okunabilirliğini bozmadan buraya aktar. Okuyan kişi cümlenin mantığını kolayca kavramalıdır.
- **[Diğer Kanal Adı]:** ...

---

🎙️ KANALLARA ÖZEL HABERLER

### [Kanal Adı] — [Konu Başlığı]
- (Ortak gündeme girmeyen konuları, yine bağlamını ve anlaşılırlığını koruyarak tam cümleler halinde buraya aktar.)

ÇIKTI FORMATI:
- Metni çok kısa özetlere dönüştürme. Akıcı bir haber bülteni/raporu dili kullan. Doğrudan "📌 ORTAK GÜNDEM" ile başla.
</görev>"""

FINAL_MERGE_PROMPT = """<batch_raporları>
{batch_reports}
</batch_raporları>

<görev>
Yukarıda kendi içinde birleştirilmiş "grup" raporları bulunmaktadır. Bunları okunabilirliği yüksek, TEK bir nihai rapora dönüştüreceksin.

ADIMLAR VE KURALLAR:
1. Girdi raporlarındaki tüm konuları tara. Sadece GERÇEKTEN AYNI somut haberi/olayı anlatan konuları nihai raporun "📌 ORTAK GÜNDEM" kısmında birleştir. Zorlama soyut başlıklar (Örn: "Devletin Rolü") yaratma.
2. Sadece tek bir kanalın bahsettiği konuları "🎙️ KANALLARA ÖZEL HABERLER" kısmında topla.
3. Girdilerdeki hiçbir bağlamı, açıklayıcı sebebi veya detaylı ifadeyi silme. 

ÇIKTI ŞABLONU:

📌 ORTAK GÜNDEM

### [Spesifik Konu Başlığı]
**Olayın Arka Planı:** (Girdi raporlarındaki arka plan metinlerini birleştirerek, olayı bilmeyen birine anlatır gibi doyurucu ve net bir paragraf oluştur.)
**Kanal Katkıları:**
- **[Kanal Adı]:** İlgili kanalın tüm maddeleri eksiksiz ve mantıklı bir bütünlük içinde taşınır. Cümleler havada kalmamalıdır.

---

🎙️ KANALLARA ÖZEL HABERLER

### [Kanal Adı] — [Konu Başlığı]
- (İlgili detaylar bağlamıyla birlikte taşınır.)

ÇIKTI KURALLARI:
- Doğrudan "📌 ORTAK GÜNDEM" ile başla. Akıcı, profesyonel bir rapor dili kullan.
</görev>"""


# Filtre kriterleri: video raporu güncel gündem haberlerine odaklı mı?
# Tutucu bias — tereddütteyse TUT, sadece bariz olarak gündem dışı içerik ATILSIN.
INTERESTS = """
Filtreleme kriterleri: video raporu güncel gündem haberlerine odaklı mı?

TUT (güncel gündem):
- Belirli, somut güncel siyasi olaylar — yeni kararlar, açıklamalar, atamalar, istifalar
- Türkiye siyaseti, dış politikası, ekonomisi — son haftalardaki gelişmeler
- Uluslararası çatışmalar, savaşlar, barış görüşmeleri (güncel)
- Ekonomik krizler, döviz/faiz hareketleri, makroekonomik gelişmeler
- Soruşturmalar, davalar, mahkeme kararları (kamu yararı olan)
- Yolsuzluk, skandallar, ifşalar
- Önemli sosyal/siyasi hareketler, protestolar
- Yorumcu/uzmanın BELİRLİ bir güncel olaya odaklanan analizleri

AT (güncel gündem değil):
- Genel teorik veya tarihsel analizler — "finans kapital nedir", "Osmanlı nasıl çöktü" gibi belirli güncel olaya bağlı olmayan içerikler
- Felsefi/ideolojik söyleşiler, zamansız tartışmalar
- Sanatçı, oyuncu, şarkıcı, sporcu söyleşileri, magazin, biyografik içerik
- Kişisel hikayeler, anılar, hayata bakış, motivasyon sohbetleri
- Açıklayıcı/eğitim içeriği (güncel olaya bağlı değilse)
- Spor analizleri, eğlence, lifestyle, sağlık tavsiyeleri
- Reklam, sponsor içeriği, kanal duyuruları

Tereddütteyse TUT — sadece bariz olarak gündem dışı olanları AT.
""".strip()


def ts():
    """Returns current timestamp string for logging."""
    return datetime.now().strftime("%H:%M:%S")


def log(msg, indent=0):
    """Timestamped log line with flush."""
    prefix = "  " * indent
    print(f"[{ts()}] {prefix}{msg}", flush=True)


def is_short(video_id):
    res = requests.get(
        f"https://www.googleapis.com/youtube/v3/videos?part=contentDetails&id={video_id}&key={YOUTUBE_API_KEY}"
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
        try:
            c_res = requests.get(
                f"https://www.googleapis.com/youtube/v3/channels?part=contentDetails&forHandle={handle}&key={YOUTUBE_API_KEY}"
            ).json()
            uploads_id = c_res["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            res = requests.get(
                f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId={uploads_id}&maxResults=5&key={YOUTUBE_API_KEY}"
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
                            "duration": duration
                        })
        except Exception as e:
            log(f"❌ HATA [{name}]: {e}")
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


def call_gemini(prompt, max_tokens=24000, label="Gemini", response_schema=None):
    """Shared helper: tries each API key in order, returns text or None.
    If response_schema is provided, the model is forced to return JSON matching the schema."""
    prompt_chars = len(prompt)
    log(f"🤖 [{label}] Gemini çağrısı (prompt: {prompt_chars:,} karakter, max_output: {max_tokens:,} token)", indent=1)

    config_kwargs = {
        "max_output_tokens": max_tokens,
        "thinking_config": types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.HIGH
        ),
    }
    if response_schema is not None:
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = response_schema

    for key_idx, key in enumerate(GEMINI_API_KEYS, start=1):
        if not key:
            log(f"⏭️ Key #{key_idx} boş, atlanıyor", indent=2)
            continue
        log(f"🔑 Key #{key_idx} deneniyor...", indent=2)
        client = genai.Client(api_key=key)
        for attempt in range(3):
            t0 = time.time()
            try:
                log(f"📤 API isteği gönderildi (deneme {attempt+1}/3, thinking=HIGH)...", indent=2)
                response = client.models.generate_content(
                    model='gemini-3.5-flash',
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
                    log(f"✅ [{label}] Yanıt alındı ({elapsed:.1f}s, {out_chars:,} karakter{usage_info})", indent=2)
                    return response.text.strip()
                else:
                    log(f"⚠️ [{label}] Boş yanıt geldi ({elapsed:.1f}s)", indent=2)
            except Exception as e:
                elapsed = time.time() - t0
                err_str = str(e)
                if "RESOURCE_EXHAUSTED" in err_str:
                    log(f"⛔ Key #{key_idx} kotası dolu ({elapsed:.1f}s), sıradaki key'e geçiliyor", indent=2)
                    break
                wait = (attempt + 1) * 30
                log(f"❌ API hatası ({elapsed:.1f}s, deneme {attempt+1}/3): {err_str[:200]}", indent=2)
                log(f"⏳ {wait} saniye bekleniyor...", indent=2)
                time.sleep(wait)
    log(f"❌ [{label}] Tüm key'ler ve denemeler tükendi", indent=1)
    return None


def analyze_video(video, transkript):
    prompt = PROMPT_TEMPLATE.format(
        channel_name=video["name"],
        video_title=video["title"],
        transkript=transkript
    )
    label = f"{video['name'][:20]} özet"
    result = call_gemini(prompt, max_tokens=16000, label=label)
    if result:
        rapor_match = re.search(r"<rapor>(.*?)</rapor>", result, re.DOTALL)
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
        result = call_gemini(prompt, max_tokens=48000, label="KONSOLİDASYON")
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
        result = call_gemini(prompt, max_tokens=32000, label=f"Batch {i}/{len(batches)}")
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
    final = call_gemini(prompt, max_tokens=48000, label="FİNAL BİRLEŞTİRME")
    if final:
        log(f"✅ Final birleştirme tamamlandı ({len(final):,} karakter)")
        return final
    log(f"⚠️ Final birleştirme başarısız, batch çıktıları yan yana gönderiliyor")
    return "\n\n---\n\n".join(batch_outputs)


def filter_video_reports(items):
    """Drop video reports that don't match INTERESTS. items: list of {video, report}.
    Returns filtered list. On filter failure, returns the original list (don't lose work)."""
    if not items:
        return items

    payload = "\n\n=== VIDEO AYIRICI ===\n\n".join(
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
        "Yukarıdaki video raporlarını yukarıdaki kriterlere göre değerlendir. "
        "AT kategorisine giren videoların indekslerini döndür. "
        "Tereddütteysen indekse koyma — sadece bariz gündem dışı olanları at."
    )

    log(f"")
    log(f"🔍 İLGİ FİLTRESİ başlatılıyor ({len(items)} video değerlendiriliyor)")
    try:
        result = call_gemini(prompt, max_tokens=2000, label="FİLTRE", response_schema=schema)
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
        try:
            r = requests.post(DISCORD_URL, json=payload, timeout=30)
            if r.status_code >= 400:
                log(f"⚠️ Discord {r.status_code}: {r.text[:200]}", indent=1)
            chunks_sent += 1
        except Exception as e:
            log(f"❌ Discord gönderim hatası: {e}", indent=1)
        time.sleep(0.5)
    log(f"✅ Discord gönderimi tamamlandı ({chunks_sent} parça)")


def strip_markdown(text):
    """Convert markdown to decorated plain text: underlined headings, UPPERCASE labels, • bullets."""
    text = re.sub(r'^\*\s+', '- ', text, flags=re.MULTILINE)

    def heading_repl(m):
        title = m.group(1).strip()
        underline = "─" * min(len(title), 60)
        return f"\n{title}\n{underline}\n"

    text = re.sub(r'^#{1,6}\s+(.+)$', heading_repl, text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'^Olay:', 'OLAY:', text, flags=re.MULTILINE)
    text = re.sub(r'^Kanal katkıları:', 'KANAL KATKILARI:', text, flags=re.MULTILINE)
    text = re.sub(r'^- ', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^(?!• )(.+)\n(?=• )', r'\1\n\n', text, flags=re.MULTILINE)
    text = re.sub(r'(^• .+)\n(?=• )', r'\1\n\n', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def upload_report_to_discord(report_text, filename, message):
    """POST a final message with the report attached as a .md file. No ping (already fired on first chunk)."""
    log(f"📎 Discord'a dosya ekleniyor: {filename} ({len(report_text):,} karakter)...")
    payload = {"content": message}
    files = {
        "payload_json": (None, json.dumps(payload), "application/json"),
        "file": (filename, report_text.encode("utf-8"), "text/markdown"),
    }
    try:
        r = requests.post(DISCORD_URL, files=files, timeout=60)
        if r.status_code >= 400:
            log(f"⚠️ Discord {r.status_code}: {r.text[:200]}", indent=1)
        else:
            log(f"✅ Discord dosya yükleme tamamlandı")
    except Exception as e:
        log(f"❌ Discord dosya yükleme hatası: {e}", indent=1)


if __name__ == "__main__":
    run_start = time.time()
    log(f"🚀 BAŞLATILIYOR")

    videos = get_latest_video_list()

    if not videos:
        log("Son 24 saatte yeni video bulunamadı.")
    else:
        log(f"📋 Bulunan videolar ({len(videos)} adet):")
        for v in videos:
            log(f"  - [{v['name']}] {v['title']} ({v['duration']})")

        analyzed = []  # her eleman: {"video": video, "report": report}

        for i, video in enumerate(videos):
            video_start = time.time()
            log(f"")
            log(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            log(f"[{i+1}/{len(videos)}] İşleniyor: [{video['name']}] {video['title']}")
            log(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

            transkript = transkript_cek(video["video_id"])
            if not transkript:
                log(f"⚠️ Transkript alınamadı, video atlanıyor", indent=1)
                continue

            log(f"📝 İlk 300 karakter: {transkript[:300]}...", indent=1)
            report = analyze_video(video, transkript)
            video["transkript"] = transkript
            analyzed.append({"video": video, "report": report})
            video_elapsed = time.time() - video_start
            log(f"✅ Video tamamlandı ({video_elapsed:.1f}s, rapor: {len(report):,} karakter)", indent=1)
            time.sleep(2)

        if not analyzed:
            log("❌ Hiçbir video işlenemedi.")
        else:
            analyzed = filter_video_reports(analyzed)
            if not analyzed:
                log("❌ İlgi filtresi tüm videoları eledi, rapor üretilmiyor.")

        if analyzed:
            all_reports = [
                f"**{idx}.** 📺 **[{it['video']['name']}]** — {it['video']['title']} `({it['video']['duration']})`\n\n{it['report']}"
                for idx, it in enumerate(analyzed, 1)
            ]
            processed_videos = [it["video"] for it in analyzed]

            current_date = datetime.now().strftime("%d.%m.%Y")

            final_report_body = consolidate_reports(all_reports)

            videos_list = "\n".join(
                f"- **[{v['name']}]** {v['title']} `({v['duration']})`"
                for v in processed_videos
            )
            full_report = (
                f"📅 **{current_date}**\n\n"
                f"📺 **İncelenen Videolar ({len(processed_videos)} adet):**\n"
                f"{videos_list}\n\n"
                f"---\n\n"
                f"{final_report_body}"
            )

            send_to_discord(full_report)

            upload_report_to_discord(
                strip_markdown(full_report),
                "Rapor.md",
                f"**Youtube {datetime.now().strftime('%d %m %Y')}**",
            )

            total_elapsed = time.time() - run_start
            mins = int(total_elapsed // 60)
            secs = int(total_elapsed % 60)
            log(f"")
            log(f"🏁 İŞLEM TAMAMLANDI")
            log(f"   • {len(all_reports)} video işlendi")
            log(f"   • Toplam süre: {mins} dk {secs} sn")
            log(f"   • Konsolide rapor Discord'a gönderildi")

            transcript_filename = f"transkriptler_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            total_chars = 0
            with open(transcript_filename, "w", encoding="utf-8") as f:
                for i, v in enumerate(processed_videos, 1):
                    text = v.get("transkript", "(transkript yok)")
                    total_chars += len(text)
                    f.write("=" * 70 + "\n")
                    f.write(f"[{i}/{len(processed_videos)}] {v['name']} — {v['title']} ({v['duration']})\n")
                    f.write(f"https://www.youtube.com/watch?v={v['video_id']}\n")
                    f.write("=" * 70 + "\n\n")
                    f.write(text)
                    f.write("\n\n")
            log(f"📝 Transkriptler dosyaya yazıldı: {transcript_filename} ({total_chars:,} karakter)")
