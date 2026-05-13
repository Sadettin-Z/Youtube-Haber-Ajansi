import os
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
    "Erdem Atay": "@erdematayveryansintv",
    "Veryansın Tv": "@VeryansinTv",
    "Onlar TV": "@onlartv",
    "Yılmaz Özdil": "@yilmaz_ozdil",
    "Cem Gürdeniz": "@cemgurdenizz",
    "Caspian Report": "@CaspianReport",
    "Cüneyt Özdemir": "@cuneytozdemir",
}

PROMPT_TEMPLATE = """<transkript>
Kanal: {channel_name}
Video Başlığı: {video_title}

{transkript}
</transkript>

<görev>
Yukarıdaki transkriptten, haber değeri taşıyan tüm bilgileri içeren bir yapılandırılmış rapor üret.

Raporda mutlaka yer alacaklar:
- Her somut iddia, veri, sayı, tarih, isim, kurum, yer
- Konuklar veya sunucu tarafından yapılan her yorum — yorumu yapan kişinin adıyla birlikte ("X'in yorumu: ...")
- Olayların aktarıldığı bağlam ve kaynak gösterimleri

Hariç tutulacaklar (sadece bunlar):
- Sponsor mesajları, reklamlar
- Selamlama, kapanış, kanal tanıtımı, abone ol çağrıları
- Yayın akışıyla ilgili teknik açıklamalar (örn. "şimdi şunu izleyeceğiz")

Format kuralları:
- Konuyla ilgili başlıklar altında grupla. Transkriptin sunum sırasını takip etmek zorunda değilsin; olay örgüsüne göre en mantıklı sırayı seç.
- Her başlık altında ilgili tüm detayları madde işaretleri veya akıcı paragraflarla aktar — hangisi daha okunaklıysa.
- Önemli isimleri ve kurumları **kalın** yap.
- Sayısal verileri (TL, %, tarih, oran) olduğu gibi koru.

Çıktı kuralları:
- Rapor doğrudan ilk başlıkla başlasın. Önsöz, giriş cümlesi, "İşte rapor:" gibi ifadeler kullanma.
- Rapor sonunda özet, kapanış cümlesi veya soru önerisi ekleme.
- Sadece raporu üret.

Yukarıdaki transkripte dayanarak şimdi raporu yaz.
</görev>"""

CONSOLIDATION_PROMPT = """<raporlar>
{combined_reports}
</raporlar>

<görev>
Yukarıdaki raporları aşağıdaki mekanik prosedüre göre tek bir konsolide rapora çevir.

═══ PROSEDÜR ═══

ADIM 1 (içsel, çıktıya yazma):
Raporlardaki her konuyu zihninde listele. Her konunun yanına o konuyu işleyen kanalları yaz. Konuları ikiye ayır:
  A) İki veya daha fazla kanalda geçen → ORTAK
  B) Sadece tek bir kanalda geçen → ÖZEL

ADIM 2 — Her ORTAK konu için, o konuyu işleyen kanalların raporlarındaki bilgileri zihninde gruplandır:
  • PAYLAŞILAN OLGULAR: Birden fazla kanalın aynı şekilde aktardığı temel bilgiler (olay, taraflar, sayılar, tarihler).
  • KANAL ÖZGÜN KATKILARI: Her kanalın o konuda diğerlerinden farklı olarak söylediği şey — kendine özgü veri, analiz, yorum, kaynak, perspektif.

ADIM 3 — Çıktıyı yaz.

═══ ÇIKTI YAPISI ═══

📌 ORTAK GÜNDEM
───────────────
[Her ORTAK konu için:]

### [Konunun başlığı]
**Olay:** [Paylaşılan olguların tamamı. Uzunluk olayın kapsamına göre belirlenir — küçük bir gelişme için kısa, çok parçalı bir olay için uzun paragraflar. Her isim, sayı, tarih ve temel iddia burada yer alır.]

**Kanal katkıları:**
- **[Kanal Adı] / [Yorumcunun adı varsa]:** [Bu kaynağın bu konuya EKLEDİĞİ özgün şey. Olay bölümünde geçen bilgileri tekrarlama — sadece bu kanalın özgün analizini, özgün verisini veya farklı perspektifini yaz.]
- **[Sonraki kanal]:** [Aynı şekilde, sadece özgün katkı.]
- [Konuyu işleyen her kanal için ayrı madde.]

[Konular arası boşluk ve `---` ayraçı.]

---

🎙️ KANALLARA ÖZEL HABERLER
───────────────────────────
[Raporlardaki orijinal kanal başlık sırasını koru. SADECE özel haberi olan kanalları listele.]

### [Orijinal başlık]
[Bu kanalın sadece kendisinde geçen konularını, orijinal raporda göründükleri detay seviyesinde aktar. Her isim, sayı, alıntı korunur.]

[Bir kanalın hiç özel haberi yoksa o kanalı bu bölümde listeleme — okur o kanalın katkısını Ortak Gündem içinde zaten görmüş olur.]

═══ KESİN KURALLAR ═══

1. Girdideki her özel isim (kişi, kurum, parti, yer), her sayı (TL, Euro, %, tarih, oran), her doğrudan alıntı ve kaynağına atfedilmiş yorum, konsolide raporda kaynağıyla birlikte en az bir kez geçmeli.
2. Hiçbir bilgi konsolide raporda birden fazla kez geçmez. Bir bilgi en doğal oturduğu bölümde tek bir yerde durur. Tekrar yok.
3. Kanal katkıları bölümünde "Olay" özetinde geçen bilgileri yeniden anlatma; sadece o kanalın özgün eklediğini yaz.
4. Bir kaynağın katkısı kısaysa kısa olsun; ama "diğer kaynakla aynı" diye atlanmaz — özgün ifade biçimi/farklı vurgu varsa o korunur.
5. Format: vurgular **kalın**, bölüm ayraçları `---`, listeler madde işaretiyle.

═══ ÇIKTI DÜZENİ ═══

Önsöz, açıklama yok. Doğrudan "📌 ORTAK GÜNDEM" başlığıyla başla.
Kapanış, özet, soru önerisi yok. Son kanalın özel haberi biter, rapor biter.

Şimdi yukarıdaki <raporlar> içeriğini kullanarak konsolide raporu üret.
</görev>"""


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
                data = response[0].get("transcript") or []
                if data:
                    transkript = " ".join([t.get("text", "") for t in data])
                    log(f"✓ Transkript alındı ({len(transkript):,} karakter, {elapsed:.1f}s)", indent=1)
                    return transkript
                else:
                    log(f"⚠️ Transkript boş geldi ({elapsed:.1f}s)", indent=1)
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


def call_gemini(prompt, max_tokens=24000, label="Gemini"):
    """Shared helper: tries each API key in order, returns text or None."""
    prompt_chars = len(prompt)
    log(f"🤖 [{label}] Gemini çağrısı (prompt: {prompt_chars:,} karakter, max_output: {max_tokens:,} token)", indent=1)

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
                    model='gemini-3-flash-preview',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=1.0,
                        max_output_tokens=max_tokens,
                        thinking_config=types.ThinkingConfig(
                            thinking_level=types.ThinkingLevel.HIGH
                        )
                    )
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
        return result
    return f"⚠️ [{video['name']}] \"{video['title']}\" — Tüm API keyleri tükendi."


def consolidate_reports(combined_reports_text):
    """Sends all individual reports to Gemini for cross-channel consolidation."""
    log(f"")
    log(f"🔄 KONSOLİDASYON başlatılıyor (toplam girdi: {len(combined_reports_text):,} karakter)")
    prompt = CONSOLIDATION_PROMPT.format(combined_reports=combined_reports_text)
    result = call_gemini(prompt, max_tokens=48000, label="KONSOLİDASYON")
    if result:
        log(f"✅ Konsolidasyon tamamlandı ({len(result):,} karakter)")
        return result
    log(f"⚠️ Konsolidasyon başarısız, bireysel raporlar gönderiliyor")
    return combined_reports_text


def send_to_discord(report):
    log(f"📤 Discord'a gönderiliyor (toplam {len(report):,} karakter)...")
    chunks_sent = 0
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
        try:
            r = requests.post(DISCORD_URL, json={"content": chunk}, timeout=30)
            if r.status_code >= 400:
                log(f"⚠️ Discord {r.status_code}: {r.text[:200]}", indent=1)
            chunks_sent += 1
        except Exception as e:
            log(f"❌ Discord gönderim hatası: {e}", indent=1)
        time.sleep(0.5)
    log(f"✅ Discord gönderimi tamamlandı ({chunks_sent} parça)")


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

        all_reports = []

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

            video_section = f"**{len(all_reports) + 1}.** 📺 **[{video['name']}]** — {video['title']} `({video['duration']})`\n\n{report}"
            all_reports.append(video_section)
            video_elapsed = time.time() - video_start
            log(f"✅ Video tamamlandı ({video_elapsed:.1f}s, rapor: {len(report):,} karakter)", indent=1)
            time.sleep(2)

        if not all_reports:
            log("❌ Hiçbir video işlenemedi.")
        else:
            current_date = datetime.now().strftime("%d.%m.%Y")
            combined_text = "\n\n---\n\n".join(all_reports)

            final_report_body = consolidate_reports(combined_text)

            full_report = f"📅 **{current_date}**\n\n{final_report_body}"
            send_to_discord(full_report)

            total_elapsed = time.time() - run_start
            mins = int(total_elapsed // 60)
            secs = int(total_elapsed % 60)
            log(f"")
            log(f"🏁 İŞLEM TAMAMLANDI")
            log(f"   • {len(all_reports)} video işlendi")
            log(f"   • Toplam süre: {mins} dk {secs} sn")
            log(f"   • Konsolide rapor Discord'a gönderildi")
