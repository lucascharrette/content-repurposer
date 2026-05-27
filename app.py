import streamlit as st
import anthropic
import os
import re
import json
import requests
import threading
from pathlib import Path
from bs4 import BeautifulSoup
import yt_dlp

# ── Config ────────────────────────────────────────────────────────────────────

USES_PER_CODE = 50
USAGE_FILE = Path(__file__).parent / "usage.json"
_lock = threading.RLock()

def get_api_key() -> str:
    try:
        return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        return os.getenv("ANTHROPIC_API_KEY", "")

def get_code_allotments() -> dict[str, int]:
    try:
        raw = st.secrets["ACCESS_CODES"]
    except Exception:
        raw = os.getenv("ACCESS_CODES", "")
    result = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            code, limit = entry.split(":", 1)
            result[code.strip()] = int(limit.strip())
        else:
            result[entry] = USES_PER_CODE
    return result

def load_usage() -> dict:
    with _lock:
        if USAGE_FILE.exists():
            try:
                return json.loads(USAGE_FILE.read_text())
            except Exception:
                pass
        return {}

def save_usage(data: dict):
    with _lock:
        USAGE_FILE.write_text(json.dumps(data, indent=2))

def get_uses_remaining(code: str) -> int:
    allotments = get_code_allotments()
    if code not in allotments:
        return 0
    return max(0, allotments[code] - load_usage().get(code, 0))

def consume_use(code: str) -> int:
    with _lock:
        usage = load_usage()
        usage[code] = usage.get(code, 0) + 1
        save_usage(usage)
    return get_uses_remaining(code)

# ── Page setup ────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Content Repurposer", page_icon="⚡", layout="wide")

st.markdown("""
<style>
    .stTextArea textarea { font-size: 14px; }
    .stTabs [data-baseweb="tab"] { font-size: 16px; font-weight: 600; }
</style>
""", unsafe_allow_html=True)

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "access_code" not in st.session_state:
    st.session_state.access_code = ""
if "last_output" not in st.session_state:
    st.session_state.last_output = None

# ── Password gate ─────────────────────────────────────────────────────────────

if not st.session_state.authenticated:
    st.title("⚡ Content Repurposer")
    st.markdown("---")
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.subheader("Enter your access code")
        st.caption("Purchase access to get your code.")
        code_input = st.text_input("Access code", type="password", placeholder="XXXX-XXXX")
        if st.button("Unlock", type="primary", use_container_width=True):
            code = code_input.strip()
            if code in get_code_allotments():
                if get_uses_remaining(code) > 0:
                    st.session_state.authenticated = True
                    st.session_state.access_code = code
                    st.rerun()
                else:
                    st.error("This access code has no uses remaining. Purchase a new one to continue.")
            else:
                st.error("Invalid access code. Double-check and try again.")
    st.stop()

# ── Content fetching ──────────────────────────────────────────────────────────

YOUTUBE_REGEX = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|v/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)

def extract_youtube_id(url: str) -> str | None:
    m = YOUTUBE_REGEX.search(url)
    return m.group(1) if m else None

def fetch_youtube_transcript(video_id: str) -> str | None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en-US", "en-GB"],
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Prefer manual subtitles, fall back to auto-generated
        for caption_source in [info.get("subtitles", {}), info.get("automatic_captions", {})]:
            for lang in ["en", "en-US", "en-GB"]:
                tracks = caption_source.get(lang, [])
                json3_track = next((t for t in tracks if t.get("ext") == "json3"), None)
                if not json3_track:
                    continue
                resp = requests.get(json3_track["url"], timeout=15)
                resp.raise_for_status()
                data = resp.json()
                parts = []
                for event in data.get("events", []):
                    for seg in event.get("segs", []):
                        text = seg.get("utf8", "")
                        if text and text != "\n":
                            parts.append(text)
                if parts:
                    return " ".join(parts)
        return None
    except Exception as e:
        print(f"YouTube transcript error: {type(e).__name__}: {e}")
        return None

def fetch_url_content(url: str) -> tuple[str | None, str]:
    """Returns (content, source_label)."""
    yt_id = extract_youtube_id(url)
    if yt_id:
        transcript = fetch_youtube_transcript(yt_id)
        if transcript:
            return transcript, "YouTube transcript"
        return None, "YouTube (transcript unavailable)"
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = [line.strip() for line in text.splitlines() if len(line.strip()) > 40]
        return "\n".join(lines[:200]), "Article"
    except Exception:
        return None, "Article (fetch failed)"


# ── AI generation ─────────────────────────────────────────────────────────────

MAX_INPUT_WORDS = 3500

def smart_truncate(content: str) -> tuple[str, bool]:
    """Truncate to a reasonable size, keeping start + sampled middle + end."""
    words = content.split()
    if len(words) <= MAX_INPUT_WORDS:
        return content, False
    head_size = int(MAX_INPUT_WORDS * 0.5)
    tail_size = int(MAX_INPUT_WORDS * 0.3)
    middle_size = MAX_INPUT_WORDS - head_size - tail_size
    middle_start = len(words) // 2 - middle_size // 2
    head = " ".join(words[:head_size])
    middle = " ".join(words[middle_start:middle_start + middle_size])
    tail = " ".join(words[-tail_size:])
    return f"{head}\n\n[... middle section ...]\n\n{middle}\n\n[... continuing ...]\n\n{tail}", True


def repurpose_content(content: str, tone: str) -> str:
    client = anthropic.Anthropic(api_key=get_api_key())

    content, _ = smart_truncate(content)

    tone_guide = {
        "Professional": "formal, authoritative, and polished",
        "Casual & Fun": "conversational, energetic, and relatable",
        "Educational": "clear, informative, and easy to understand",
    }[tone]

    prompt = f"""You are an expert content marketer. Repurpose the content below into 4 high-performing formats.
Tone: {tone_guide}

ORIGINAL CONTENT:
{content}

Produce exactly the 4 sections below, using the exact headers shown. No extra commentary.

---THREAD---
Write a Twitter/X thread (8 to 12 tweets, numbered 1/, 2/, 3/, etc.).
- Tweet 1 must be a scroll-stopping hook (curiosity, bold claim, or surprising stat).
- Each tweet must stand alone but build on the previous one.
- Each tweet under 280 characters.
- Last tweet should include a soft CTA (reply, follow, share).

---INSTAGRAM---
Write an Instagram caption.
- First line: a punchy hook that fits in the preview (under 125 characters).
- Body: 4-7 short paragraphs with line breaks between them.
- Use emojis sparingly (2-4 total) where natural.
- End with a question to drive comments.
- After "---HASHTAGS---" on its own line, give 15-20 relevant hashtags (mix of broad and niche, no banned tags).

---VIDEO SCRIPT---
Write a short-form video script (TikTok / Reels / Shorts, 30-45 seconds total).
HOOK (0-3 sec): [one punchy line designed to stop the scroll]
CONTENT (3-40 sec):
- [bullet 1: key insight]
- [bullet 2: supporting point or example]
- [bullet 3: surprising or contrarian angle]
- [bullet 4: payoff / aha moment]
CTA (40-45 sec): [one clear call to action]
ON-SCREEN TEXT IDEAS: [3-5 short text overlay suggestions, comma separated]

---NEWSLETTER---
Write an email newsletter package.
SUBJECT LINE OPTIONS (give 5, each under 50 chars):
1. [option 1]
2. [option 2]
3. [option 3]
4. [option 4]
5. [option 5]

PREVIEW TEXT (under 90 chars): [one line]

BODY:
[Greeting]

[2-3 short paragraphs that teach the core insight and tease more. Include one specific takeaway the reader can use immediately.]

[Sign off]"""

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        return stream.get_final_text()


def parse_sections(text: str) -> dict:
    markers = ["---THREAD---", "---INSTAGRAM---", "---VIDEO SCRIPT---", "---NEWSLETTER---"]
    sections = {}
    for i, marker in enumerate(markers):
        start = text.find(marker)
        if start == -1:
            continue
        start += len(marker)
        end = len(text)
        for next_marker in markers[i + 1:]:
            pos = text.find(next_marker)
            if pos != -1:
                end = pos
                break
        sections[marker] = text[start:end].strip()
    return sections


# ── UI ────────────────────────────────────────────────────────────────────────

st.title("⚡ Content Repurposer")
st.caption("Turn any article, blog post, or YouTube video into a Twitter thread, Instagram caption, video script, and email newsletter — in seconds.")

remaining = get_uses_remaining(st.session_state.access_code)

with st.sidebar:
    st.header("Settings")
    tone = st.selectbox("Tone", ["Casual & Fun", "Professional", "Educational"])
    st.divider()
    st.markdown("**How to use**")
    st.markdown("1. Paste text, an article URL, or a YouTube link\n2. Choose a tone\n3. Click **Repurpose**\n4. Copy your content from each tab")
    st.divider()
    st.caption(f"Uses remaining: **{remaining}**")
    st.caption("Powered by Claude (Anthropic)")

if remaining <= 0:
    st.error("You have no uses remaining on this access code. Purchase a new one to continue.")
    st.stop()

input_method = st.radio("Input method", ["Paste text", "URL or YouTube link"], horizontal=True)

content = None

if input_method == "Paste text":
    raw = st.text_area(
        "Your content",
        height=280,
        placeholder="Paste a blog post, article, video transcript, or any text here...",
    )
    if raw.strip():
        content = raw.strip()
else:
    url = st.text_input("URL or YouTube link", placeholder="https://example.com/article  or  https://youtube.com/watch?v=...")
    if url.strip():
        with st.spinner("Fetching content..."):
            fetched, source = fetch_url_content(url.strip())
        if fetched:
            content = fetched
            word_count = len(content.split())
            st.success(f"✅ Fetched {source} — ~{word_count} words.")
            with st.expander("Preview fetched content"):
                st.text(content[:1000] + ("..." if len(content) > 1000 else ""))
        else:
            if "YouTube" in source:
                st.error(
                    "Couldn't fetch this transcript automatically. "
                    "**Quick fix:** Open the video on YouTube → click the **\"...\"** menu under the video → **\"Show transcript\"** → "
                    "copy the text → switch to **\"Paste text\"** above and paste it in."
                )
            else:
                st.error("Couldn't fetch that URL. Try pasting the text directly instead.")

st.divider()

col1, col2 = st.columns([1, 4])
with col1:
    generate = st.button("⚡ Repurpose", type="primary", use_container_width=True)

if generate:
    if not content:
        st.error("Paste some content or enter a URL above.")
    else:
        _, was_truncated = smart_truncate(content)
        if was_truncated:
            st.info(f"📝 Content is long ({len(content.split()):,} words) — we'll use a smart sample to keep it fast.")

        try:
            with st.spinner("⚡ Generating your content..."):
                raw_output = repurpose_content(content, tone)
            sections = parse_sections(raw_output)
            new_remaining = consume_use(st.session_state.access_code)
            st.session_state.last_output = {
                "sections": sections,
                "remaining": new_remaining,
            }
            st.rerun()
        except anthropic.AuthenticationError:
            st.error("Server configuration error. Contact support.")
        except anthropic.RateLimitError:
            st.error("Rate limit hit. Wait a moment and try again.")
        except Exception as e:
            st.error(f"Something went wrong: {e}")

if st.session_state.last_output:
    out = st.session_state.last_output
    sections = out["sections"]
    st.success(f"✅ Done! ({out['remaining']} uses remaining)")

    tab1, tab2, tab3, tab4 = st.tabs([
        "🧵 Twitter Thread",
        "📸 Instagram",
        "🎬 Video Script",
        "📧 Newsletter",
    ])
    with tab1:
        st.text_area("Twitter/X Thread", sections.get("---THREAD---", ""), height=420)
    with tab2:
        st.text_area("Instagram Caption + Hashtags", sections.get("---INSTAGRAM---", ""), height=420)
    with tab3:
        st.text_area("Short-form Video Script", sections.get("---VIDEO SCRIPT---", ""), height=420)
    with tab4:
        st.text_area("Newsletter (Subjects + Body)", sections.get("---NEWSLETTER---", ""), height=420)
