# ⚡ Content Repurposer

Turn any article, blog post, or YouTube video into:
- 🧵 A scroll-stopping Twitter/X thread
- 📸 An Instagram caption with hashtags
- 🎬 A TikTok / Reels / Shorts video script
- 📧 An email newsletter (subject lines + body copy)

Powered by Claude AI.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Create `.streamlit/secrets.toml` with:
   ```toml
   ANTHROPIC_API_KEY = "sk-ant-your-key"
   ACCESS_CODES = "YOUR-CODE-1, YOUR-CODE-2"
   ```

3. Run:
   ```
   streamlit run app.py
   ```

## Tech stack
- Streamlit (UI)
- Anthropic Claude (AI)
- yt-dlp (YouTube transcripts)
- BeautifulSoup (article scraping)
