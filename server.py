"""
Punji the Labrador — YouTube Metadata MCP Server
=================================================
Exposes four MCP tools to Claude Desktop:

  1. fetch_video_data        — fetch YouTube video data + SOP rules (read-only)
  2. authenticate_youtube    — run OAuth 2.0 browser flow, save token.json
  3. update_video_metadata   — validate metadata + return confirmation token (NO write yet)
  4. confirm_metadata_update — execute the actual YouTube API write after user confirms

Architecture decision: this server does NOT call the Anthropic/Claude API.
Claude Desktop's built-in Claude generates the metadata. This server only
fetches data (YouTube API key) and writes data (OAuth 2.0).
"""

import sys
import os
import re
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from mcp.server.fastmcp import FastMCP

# Always load .env from the project directory (same folder as server.py)
# This works even when Claude Desktop launches the server from a different working directory
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# All logging → stderr only. stdout is reserved for JSON-RPC protocol.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(PROJECT_ROOT, "token.json")
CLIENT_SECRETS_PATH = os.path.join(PROJECT_ROOT, "client_secrets.json")
UPDATE_LOG_PATH = os.path.join(PROJECT_ROOT, ".claude", "UPDATE_LOG.md")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
QUEUE_PATH = os.path.join(PROJECT_ROOT, "queue.json")

# Create logs directory on startup
os.makedirs(LOGS_DIR, exist_ok=True)

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# Channel handle for auto-fetching latest videos
CHANNEL_HANDLE = "punji_the_labrador"

# If a video already has more than this many characters in description → stop queue
EXISTING_DESCRIPTION_THRESHOLD = 100

# ---------------------------------------------------------------------------
# PENDING CONFIRMATION STORE
# In-memory dict: token → {video_id, title, description, tags, original_title, created_at}
# Tokens expire after CONFIRMATION_TOKEN_TTL_SECONDS seconds.
# ---------------------------------------------------------------------------

_pending_updates: dict[str, dict[str, Any]] = {}
CONFIRMATION_TOKEN_TTL_SECONDS = 300  # 5 minutes

# ---------------------------------------------------------------------------
# SOP CONSTANTS
# ---------------------------------------------------------------------------

FORBIDDEN_PHRASES = [
    "you won't believe",
    "you wont believe",
    "shocking",
    "this will make you cry",
    "unbelievable",
    "you need to see this",
    "watch before it's deleted",
    "watch before its deleted",
    "gone wrong",
    "gone sexual",
    "fake rescue",
    "emotional rescue",
    "must watch",
    "jaw dropping",
]

REQUIRED_TAG = "punji the labrador"
MAX_TAG_LENGTH = 30
MIN_TAGS = 15
MAX_TAGS = 20
MIN_TITLE_LEN = 60
MAX_TITLE_LEN = 70
MIN_DESC_LEN = 1200
MAX_DESC_LEN = 3500

# Emoji unicode ranges (broad coverage)
EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F"   # emoticons
    "\U0001F300-\U0001F5FF"    # misc symbols & pictographs
    "\U0001F680-\U0001F6FF"    # transport & map
    "\U0001F1E0-\U0001F1FF"    # flags
    "\U00002702-\U000027B0"    # dingbats
    "\U000024C2-\U0001F251"    # enclosed chars
    "\U0001F900-\U0001F9FF"    # supplemental symbols
    "\U0001FA00-\U0001FA6F"    # chess symbols
    "\U0001FA70-\U0001FAFF"    # symbols and pictographs extended
    "\U00002600-\U000026FF"    # misc symbols
    "]+",
    flags=re.UNICODE,
)

# Devanagari Unicode block — U+0900 to U+097F
HINDI_RE = re.compile(r"[\u0900-\u097F]")

# ---------------------------------------------------------------------------
# SOP REFERENCE TEXT (returned to Claude chat so it can generate metadata)
# ---------------------------------------------------------------------------

CHANNEL_SOP = """
══════════════════════════════════════════════════════
CHANNEL: Punji the Labrador (@punji_the_labrador)
══════════════════════════════════════════════════════
• 9-year-old Labrador Retriever, India
• Owner: "Mom" (Rachna Sachan) — runs daily welfare operations
• Mission: Algorithmic Philanthropy — YouTube revenue funds street dog welfare
• 102K+ subscribers · 85.3M total views · 74%+ View-Through Rate
• Audience: 95.7% India · 60.5% Male · 39.1% Female
• Content: Dog vlogs, funny moments, street dog welfare — ALL REAL, NEVER STAGED

WELFARE CONTEXT (weave naturally into metadata):
• Daily morning feeding of neighbourhood street dogs (rice + protein)
• On-demand sanctuary: street dogs come to the house freely
• Geriatric tubectomy program for old female dogs (7+ years)
• Street dogs = "Punji's Friends" — dignity framing, never pity framing
• Joy-based, non-performative — welfare is shown, never performed for donations

══════════════════════════════════════════════════════
SOP — TITLE RULES (HARD CONSTRAINTS)
══════════════════════════════════════════════════════
• Length: 60–70 characters EXACTLY (count carefully)
• Must contain at least one Hindi word or phrase (Devanagari script)
• Must include exactly one relevant emoji — NOT as the first character
• Must NOT contain any ALL CAPS words (3+ letters)
• Must NOT start with a number
• Must NOT contain these forbidden phrases:
  "You won't believe", "Shocking", "Gone wrong", "Gone sexual",
  "This will make you cry", "Fake rescue", "Must watch", "Jaw dropping"
• Primary keyword must appear naturally within first 40 characters
• Formula: [Real Action/Emotion] + [Primary Keyword] | [Hindi context] [emoji]

══════════════════════════════════════════════════════
SOP — DESCRIPTION RULES (5 MANDATORY SECTIONS)
══════════════════════════════════════════════════════
Total length: 1,200–3,500 characters

Section 1 — Hook (150–200 chars):
  Emotional Hindi-English hook. No hashtags. No clickbait.

Section 2 — Story/Context (300–500 chars):
  What actually happens in the video. Natural keyword weaving.
  Real people, real dogs, real moments only.

Section 3 — About Punji (200–300 chars):
  Fixed evergreen paragraph about the channel and Punji.
  Example: "Punji is a 9-year-old Labrador who lives with his family in India..."

Section 4 — Social + Contact (100–150 chars):
  YouTube subscribe link, Instagram handle (@punji_the_labrador), contact email.

Section 5 — Tags/Hashtag Block (50–80 hashtags):
  Grouped by: primary dog tags · Hindi content tags · emotion tags · welfare tags · trending tags
  Format: #tag1 #tag2 #tag3 (space-separated hashtags)

DESCRIPTION RULES:
• Must NOT copy the title verbatim
• Must NOT keyword stuff unnaturally
• Language: Hindi + English naturally mixed
• Must NOT include any analytics numbers — no subscriber count, view count,
  like count, or comment count anywhere in the description.
  Wrong: "With 102K subscribers and 85M views..."
  Wrong: "This video has 50K views and 2K likes!"
  Right: Describe the content and story only — never performance metrics.
• Must NOT mention the video's length or duration anywhere in title, description, or tags.
  Wrong: "Watch this 5 minute video", "In this 3:42 video...", "#shortvideo", "#longvideo"
  Right: Never reference how long the video is — let viewers discover that themselves.

══════════════════════════════════════════════════════
SOP — TAG RULES (15–20 TAGS)
══════════════════════════════════════════════════════
• Total: 15–20 tags (not fewer, not more)
• "punji the labrador" MUST always be one of the tags
• No duplicate tags (case-insensitive)
• No tag longer than 30 characters
• Tag mix required:
  - Broad: labrador, dog, cute dog, dog vlog
  - Niche: street dog india, labrador india, dog india
  - Long-tail: funny labrador reaction hindi, labrador daily life india
  - Channel: punji the labrador
══════════════════════════════════════════════════════

══════════════════════════════════════════════════════
SEO OPTIMIZATION RULES — MANDATORY, APPLY ALONGSIDE BRAND SOP
══════════════════════════════════════════════════════

TITLE SEO RULES:
• Primary keyword must appear in the first 40 characters
  (YouTube truncates titles at ~55 chars on mobile search results)
• Use high-intent power words naturally: "first time", "every day",
  "real", "caught on camera", "you won't see this anywhere"
• Numbers drive CTR: "3 साल में पहली बार..." — use when truthful
• Questions drive CTR: "Does Punji really...?" — use when truthful
• Format target: [Primary Keyword] — [Emotional Hook] [one emoji]
• Test yourself: if someone searched this keyword, would they click this title?

DESCRIPTION SEO RULES:
• First 150 characters = "above the fold" on mobile (shown before "Show more")
  These 150 chars MUST contain: the primary keyword + an emotional hook sentence
  Wrong: "Welcome to Punji the Labrador channel. In this video..."
  Right:  "Punji the Labrador had the cutest reaction when he saw..."
• Repeat the primary keyword naturally 2-3 times in the first 500 characters
• If video duration is longer than 60 seconds, ADD chapter timestamps:
    00:00 Introduction
    00:45 [Chapter name]
    01:30 [Chapter name]
    ...
  Chapters are indexed by Google and YouTube as independent search results.
  This alone can double discovery for long-form videos.
• Add secondary and related keywords naturally in the Story/Context section
• Do NOT keyword stuff — write for humans, not bots

TAG SEO RULES:
• Tag ORDER matters — YouTube weights the first 3 tags most heavily
• Tag order must follow this sequence:
    1st tag: most specific long-tail keyword (e.g. "labrador dog india hindi")
    2nd tag: secondary specific keyword (e.g. "punji the labrador daily vlog")
    3rd tag: broader category (e.g. "labrador india")
    4th–14th: mix of broad, niche, emotion, and welfare tags
    Last tag: always "punji the labrador" (channel brand anchor)
• Every tag must be something a real person would type into YouTube search
• Avoid tags that are too generic to rank for: "dog", "cute", "funny" alone
  — pair them: "cute labrador india", "funny dog reaction hindi"

══════════════════════════════════════════════════════
CONTENT INFERENCE RULE — CRITICAL
══════════════════════════════════════════════════════

The channel owner (Mom) writes the video title herself in Hindi/English.
That title is your PRIMARY and ONLY source of ground truth for the video's content.

Rules:
• NEVER ask for more information. Always generate complete metadata.
• From the title alone, infer: the scene, the emotion, the story arc, the characters.
• If transcript context is provided, use it to enrich — not contradict — the title.
• If the title says "Punji ne pehli baar barish dekhi", infer:
    Scene: outdoors, monsoon, first experience
    Emotion: wonder, excitement, confusion
    Story arc: Punji's innocent discovery of rain
    Keywords: labrador in rain, dog first rain reaction india
  Then write the full description as if you witnessed this moment.
• Always generate. Never stall. The title tells you everything you need.

══════════════════════════════════════════════════════
LANGUAGE RULE — MANDATORY
══════════════════════════════════════════════════════

• ALL metadata (title, description, tags) must be in ENGLISH ONLY.
• English must be simple, warm, and easy to understand for any audience.
• Avoid complex vocabulary, formal writing, or academic tone.
  Wrong: "Punji exhibits unprecedented enthusiasm upon encountering precipitation"
  Right:  "Punji sees rain for the first time and can not stop jumping!"
• Hindi words are NOT allowed anywhere in title, description, or tags.
• Exception: proper nouns are fine — "Punji", "Rachna", "Mumbai", "Delhi"
• The channel video itself is in Hindi — the metadata is in English.
  This is intentional for maximum global + India English search reach.

══════════════════════════════════════════════════════
SHORTS METADATA RULE — CRITICAL FOR SHORT VIDEOS
══════════════════════════════════════════════════════

YouTube Shorts (videos 60 seconds or under) on this channel use BORROWED AUDIO
from other creators — trending Bollywood songs, viral dialogue, popular sounds.

This means:
• The transcript of a Short = audio from another creator, NOT Punji's story
• You must NEVER treat the transcript as the Short's story
• You must use the transcript ONLY to extract: mood, emotion, tone, energy level

The 80/20 rule for Shorts metadata:
• 80% — from the title (what actually happens visually, Mom's creative intent)
• 20% — from the audio transcript flavour (the emotional tone of the sound)

How to apply the 20% flavour:
• Audio is a sad Bollywood song about longing → weave loyalty, waiting, love into description
• Audio is an energetic/hype track → make title and description feel exciting and fast
• Audio is funny/comedic dialogue → keep tone light, playful, and humorous
• Audio is a devotional/peaceful song → use calm, warm, heartwarming tone
• NEVER copy audio lyrics into description or tags — flavour only, not content
══════════════════════════════════════════════════════
"""

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def extract_video_id(url_or_id: str) -> str | None:
    """
    Extract a YouTube video ID from any URL format or bare 11-character ID.

    Handles:
      - https://www.youtube.com/watch?v=VIDEO_ID
      - https://youtu.be/VIDEO_ID
      - https://youtube.com/shorts/VIDEO_ID
      - https://www.youtube.com/embed/VIDEO_ID
      - VIDEO_ID  (bare 11-character ID)
    """
    s = url_or_id.strip()

    # Bare video ID (exactly 11 allowed chars)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s

    # youtu.be/VIDEO_ID
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)

    # youtube.com/shorts/VIDEO_ID
    m = re.search(r"youtube\.com/shorts/([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)

    # youtube.com/watch?v=VIDEO_ID  (any extra query params OK)
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)

    # youtube.com/embed/VIDEO_ID
    m = re.search(r"youtube\.com/embed/([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)

    return None


def get_video_data(video_id: str) -> dict[str, Any]:
    """
    Fetch full video metadata from YouTube Data API v3.

    Returns: video_id, title, description, tags, duration, category_id,
             view_count, like_count, comment_count, published_at.

    Raises:
        ValueError  — video not found, private, or invalid ID
        RuntimeError — quota exceeded, missing API key, or network error
    """
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "YOUTUBE_API_KEY environment variable is not set. "
            "Add it to Claude Desktop config env block or .env file."
        )

    try:
        youtube = build("youtube", "v3", developerKey=api_key)
        response = youtube.videos().list(
            part="snippet,statistics,contentDetails",
            id=video_id,
        ).execute()
    except HttpError as e:
        status = e.resp.status
        if status == 403 and "quotaExceeded" in str(e):
            raise RuntimeError(
                "YouTube API daily quota exceeded. Quota resets at midnight Pacific Time."
            ) from e
        if status == 400:
            raise ValueError(f"Invalid video ID '{video_id}'.") from e
        raise RuntimeError(f"YouTube API error (HTTP {status}): {e}") from e
    except Exception as e:
        raise RuntimeError(f"Failed to connect to YouTube API: {e}") from e

    items = response.get("items", [])
    if not items:
        raise ValueError(
            f"Video '{video_id}' not found. It may be private, deleted, or the ID is incorrect."
        )

    item = items[0]
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    details = item.get("contentDetails", {})

    return {
        "video_id": video_id,
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "tags": snippet.get("tags", []),
        "duration": details.get("duration", ""),       # ISO 8601, e.g. "PT3M42S"
        "category_id": snippet.get("categoryId", "22"),
        "published_at": snippet.get("publishedAt", ""),
        "view_count": stats.get("viewCount", "N/A"),
        "like_count": stats.get("likeCount", "N/A"),
        "comment_count": stats.get("commentCount", "N/A"),
    }


def get_oauth_credentials() -> Credentials:
    """
    Load OAuth2 credentials from token.json, refreshing if expired.

    Raises:
        FileNotFoundError — client_secrets.json not present
        RuntimeError      — token.json not found (user must authenticate first)
    """
    if not os.path.exists(CLIENT_SECRETS_PATH):
        raise FileNotFoundError(
            f"client_secrets.json not found at {CLIENT_SECRETS_PATH}. "
            "Download it from Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client IDs."
        )

    creds: Credentials | None = None

    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, YOUTUBE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("OAuth token expired — refreshing silently.")
            creds.refresh(Request())
            _save_token(creds)
            logger.info("Token refreshed and saved.")
        else:
            raise RuntimeError(
                "No valid OAuth token found. Call the authenticate_youtube tool first "
                "to complete the one-time browser authentication flow."
            )

    return creds


def _save_token(creds: Credentials) -> None:
    """Persist OAuth credentials to token.json."""
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())


def validate_metadata(title: str, description: str, tags: list[str]) -> list[str]:
    """
    Validate metadata against the full production SOP.

    Returns a list of violation strings. Empty list = fully valid.
    """
    violations: list[str] = []

    # ── TITLE ──────────────────────────────────────────────────────────────
    t_len = len(title)
    if t_len < MIN_TITLE_LEN:
        violations.append(
            f"Title is {t_len} chars — minimum is {MIN_TITLE_LEN}. Got: '{title}'"
        )
    elif t_len > MAX_TITLE_LEN:
        violations.append(
            f"Title is {t_len} chars — maximum is {MAX_TITLE_LEN}. Got: '{title}'"
        )

    # Must not start with a digit
    if title and title[0].isdigit():
        violations.append(f"Title must NOT start with a number. Got: '{title[0]}'")

    # Must not contain ALL CAPS words (3+ letters; allows "OK", "TV")
    caps = re.findall(r"\b[A-Z]{3,}\b", title)
    if caps:
        violations.append(f"Title contains ALL CAPS words: {caps}")

    # Must not contain forbidden phrases (case-insensitive)
    t_low = title.lower()
    for phrase in FORBIDDEN_PHRASES:
        if phrase in t_low:
            violations.append(f"Title contains forbidden phrase: '{phrase}'")

    # Must contain at least one Hindi (Devanagari) character
    if not HINDI_RE.search(title):
        violations.append(
            "Title must contain at least one Hindi word or phrase (Devanagari script). "
            "Example: add a Hindi word naturally — e.g. 'pyaar', 'dost', etc."
        )

    # Must contain an emoji — and NOT as the first character
    emoji_matches = list(EMOJI_RE.finditer(title))
    if not emoji_matches:
        violations.append(
            "Title must include at least one relevant emoji (but NOT as the first character)."
        )
    else:
        first_char = title.strip()[0] if title.strip() else ""
        if EMOJI_RE.match(first_char):
            violations.append(
                "Title must NOT start with an emoji. Move the emoji later in the title."
            )

    # ── DESCRIPTION ────────────────────────────────────────────────────────
    d_len = len(description)
    if d_len < MIN_DESC_LEN:
        violations.append(
            f"Description is {d_len} chars — minimum is {MIN_DESC_LEN}."
        )
    elif d_len > MAX_DESC_LEN:
        violations.append(
            f"Description is {d_len} chars — maximum is {MAX_DESC_LEN}."
        )

    # Must not copy the title verbatim
    if title and title.strip() in description:
        violations.append(
            "Description must NOT copy the title verbatim. Rephrase the opening."
        )

    # Must not include analytics numbers (e.g. "102K subscribers", "85M views", "50K views")
    analytics_pattern = re.compile(
        r"\b\d+(?:\.\d+)?[KkMmBb]\s*(?:subscribers?|views?|likes?|comments?)\b",
        re.IGNORECASE,
    )
    if analytics_pattern.search(description):
        violations.append(
            "Description must NOT include analytics numbers (subscriber count, view count, "
            "like count, or comment count). Remove phrases like '102K subscribers' or '50K views'."
        )

    # Must not mention video duration/length in title, description, or tags
    duration_pattern = re.compile(
        r"\b(?:this\s+)?(?:\d+\s*(?:minute|min|second|sec|hour|hr)s?\s*(?:video|clip|short)?|"
        r"\d+:\d{2}(?:\s*(?:video|clip))?|"
        r"(?:long|short)\s*video)\b",
        re.IGNORECASE,
    )
    duration_tags = {"shortvideo", "longvideo", "shortfilm", "shortclip", "longvlog"}

    if duration_pattern.search(title):
        violations.append(
            "Title must NOT mention video length or duration (e.g. '5 minute video', '3:42'). "
            "Remove any reference to how long the video is."
        )
    if duration_pattern.search(description):
        violations.append(
            "Description must NOT mention video length or duration "
            "(e.g. 'watch this 5 minute video', 'in this 3:42 video'). "
            "Remove any reference to how long the video is."
        )
    for tag in tags:
        if tag.lower().replace(" ", "") in duration_tags:
            violations.append(
                f"Tag '{tag}' references video length — remove it. "
                "Never use tags like #shortvideo or #longvideo."
            )

    # ── TAGS ───────────────────────────────────────────────────────────────
    n_tags = len(tags)
    if n_tags < MIN_TAGS:
        violations.append(
            f"Only {n_tags} tags provided — minimum is {MIN_TAGS}."
        )
    elif n_tags > MAX_TAGS:
        violations.append(
            f"{n_tags} tags provided — maximum is {MAX_TAGS}."
        )

    # Required tag must be present
    tags_lower = [t.lower().strip() for t in tags]
    if REQUIRED_TAG not in tags_lower:
        violations.append(
            f"Tags must always include '{REQUIRED_TAG}' as one of the tags."
        )

    # No tag over 30 characters
    for tag in tags:
        if len(tag) > MAX_TAG_LENGTH:
            violations.append(
                f"Tag '{tag}' is {len(tag)} chars — maximum tag length is {MAX_TAG_LENGTH}."
            )

    # No duplicate tags (case-insensitive)
    seen: set[str] = set()
    for tag in tags_lower:
        if tag in seen:
            violations.append(f"Duplicate tag: '{tag}'")
            break
        seen.add(tag)

    # ── SEO CHECKS ──────────────────────────────────────────────────────────

    # Description: first 150 chars must not open with a generic phrase
    first_150 = description[:150].lower().strip()
    generic_openers = [
        "welcome to",
        "in this video",
        "hello everyone",
        "hi everyone",
        "hey everyone",
        "namaste",
        "subscribe to",
        "don't forget to",
        "like and subscribe",
    ]
    for opener in generic_openers:
        if first_150.startswith(opener):
            violations.append(
                f"SEO: Description starts with a generic opener: '{opener}'. "
                "The first 150 characters are shown before 'Show more' on mobile. "
                "Start with a keyword-rich emotional hook sentence instead. "
                "Example: 'Punji the Labrador had the most adorable reaction when...'"
            )
            break

    # Tags: first tag must NOT be a single generic word
    overly_generic_single_tags = {
        "dog", "dogs", "puppy", "puppies", "cute", "funny",
        "animal", "animals", "pet", "pets", "vlog", "video",
        "india", "hindi"
    }
    if tags and tags[0].lower().strip() in overly_generic_single_tags:
        violations.append(
            f"SEO: First tag '{tags[0]}' is too generic to rank for. "
            "YouTube weights the first 3 tags most. "
            "Put your most specific long-tail keyword first. "
            "Example: 'labrador dog india hindi' instead of just 'dog'."
        )

    return violations


def _video_log(video_id: str, event: str, detail: str = "") -> None:
    """
    Append a timestamped event to logs/VIDEO_ID.log.

    One log file per video — all operations (fetch, validate, update) are
    recorded there so you can see the full history of any video at a glance.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    log_path = os.path.join(LOGS_DIR, f"{video_id}.log")
    line = f"[{timestamp}] {event}"
    if detail:
        line += f"\n    {detail}"
    line += "\n"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        logger.error("Failed to write video log for %s: %s", video_id, e)


def _log_update(
    video_id: str,
    original_title: str,
    new_title: str,
    description: str,
    tags: list[str],
    status: str,
) -> None:
    """Append a record to .claude/UPDATE_LOG.md and the per-video log."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = (
        f"\n## {timestamp}\n"
        f"- Video ID: `{video_id}`\n"
        f"- URL: https://youtube.com/watch?v={video_id}\n"
        f"- Original Title: {original_title}\n"
        f"- New Title: {new_title}\n"
        f"- Description: {len(description)} chars\n"
        f"- Tags: {len(tags)} tags — {', '.join(tags[:5])}{'...' if len(tags) > 5 else ''}\n"
        f"- Status: {status}\n"
        f"---\n"
    )
    try:
        with open(UPDATE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as e:
        logger.error("Failed to write UPDATE_LOG.md: %s", e)

    # Also write to per-video log
    _video_log(
        video_id,
        f"METADATA UPDATE — {status}",
        f"Title: {new_title} | Desc: {len(description)} chars | Tags: {len(tags)}",
    )


# ---------------------------------------------------------------------------
# QUEUE HELPERS
# ---------------------------------------------------------------------------

def _save_queue(queue: dict) -> None:
    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)


def _load_queue() -> dict | None:
    if not os.path.exists(QUEUE_PATH):
        return None
    with open(QUEUE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_uploads_playlist_id(api_key: str) -> str:
    """Resolve the channel handle to its uploads playlist ID."""
    youtube = build("youtube", "v3", developerKey=api_key)
    response = youtube.channels().list(
        forHandle=CHANNEL_HANDLE,
        part="contentDetails",
    ).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError(
            f"Channel @{CHANNEL_HANDLE} not found. Check CHANNEL_HANDLE in server.py."
        )
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


# ---------------------------------------------------------------------------
# MCP SERVER
# ---------------------------------------------------------------------------

mcp = FastMCP("punji-youtube-metadata")


@mcp.tool()
def fetch_video_data(video_url: str) -> str:
    """
    Fetch YouTube video data for a Punji the Labrador video.

    Retrieves the current title, description, tags, view count, like count,
    comment count, published date, and duration from YouTube Data API v3.
    Returns all data plus the full channel SOP rules so Claude can generate
    optimised metadata in the chat.

    Args:
        video_url: YouTube video URL (any format) or bare 11-character video ID.
                   Accepted: youtube.com/watch?v=, youtu.be/, youtube.com/shorts/,
                   youtube.com/embed/, or a bare VIDEO_ID.

    Returns:
        Structured text with all video data and SOP rules for Claude to use.
    """
    video_id = extract_video_id(video_url)
    if not video_id:
        return (
            f"ERROR: Could not extract a YouTube video ID from: '{video_url}'\n\n"
            "Accepted formats:\n"
            "  • https://www.youtube.com/watch?v=VIDEO_ID\n"
            "  • https://youtu.be/VIDEO_ID\n"
            "  • https://youtube.com/shorts/VIDEO_ID\n"
            "  • VIDEO_ID  (bare 11-character ID)"
        )

    logger.info("fetch_video_data: %s", video_id)

    try:
        data = get_video_data(video_id)
    except ValueError as e:
        _video_log(video_id, "FETCH FAILED", str(e))
        return f"ERROR (Video not found): {e}"
    except RuntimeError as e:
        _video_log(video_id, "FETCH FAILED", str(e))
        return f"ERROR (YouTube API): {e}"

    _video_log(
        video_id,
        "FETCHED",
        f"Title: {data['title']} | Views: {data['view_count']} | "
        f"Desc: {len(data['description'])} chars | Tags: {len(data['tags'])}",
    )

    tags_display = (
        "\n".join(f"  {i+1}. {t}" for i, t in enumerate(data["tags"]))
        if data["tags"]
        else "  (no tags currently set)"
    )

    sep = "═" * 60

    def _fmt_stat(label: str, val: str) -> str:
        if val == "N/A":
            return f"{label}: N/A"
        try:
            return f"{label}: {int(val):,}"
        except (ValueError, TypeError):
            return f"{label}: {val}"

    lines = [
        sep,
        f"VIDEO DATA — https://youtube.com/watch?v={video_id}",
        sep,
        f"Title        : {data['title']}",
        f"Published    : {data['published_at'][:10] if data['published_at'] else 'unknown'}",
        f"Duration     : {data['duration']}",
        f"Category ID  : {data['category_id']}",
        _fmt_stat("Views        ", data["view_count"]),
        _fmt_stat("Likes        ", data["like_count"]),
        _fmt_stat("Comments     ", data["comment_count"]),
        "",
        sep,
        f"CURRENT DESCRIPTION ({len(data['description'])} chars):",
        sep,
        data["description"],
        "",
        sep,
        f"CURRENT TAGS ({len(data['tags'])} tags):",
        sep,
        tags_display,
        "",
        sep,
        f"VIDEO ID (use this in update_video_metadata): {video_id}",
        sep,
        CHANNEL_SOP,
    ]
    return "\n".join(lines)


@mcp.tool()
def fetch_trending_keywords(seed_keyword: str) -> str:
    """
    Fetch trending YouTube search suggestions for a seed keyword.

    Queries YouTube's autocomplete endpoint (no API key needed) using
    multiple suffix variations to capture a broad range of trending terms.
    Use this BEFORE generating metadata to find what real users are searching.

    Args:
        seed_keyword: A short topic phrase. Examples:
                      "labrador dog india", "cute dog hindi vlog",
                      "street dog rescue india", "labrador funny reaction"

    Returns:
        Up to 30 unique trending search terms ranked by YouTube's own
        autocomplete algorithm. Use the most relevant ones naturally in
        title (first 40 chars), description (first 500 chars), and tags.
    """
    import urllib.request
    import urllib.parse

    suffixes = [
        "",
        " a", " b", " c", " d",
        " how", " why", " what", " when",
        " india", " hindi", " 2024", " 2025",
        " क", " ल", " म",
    ]

    suggestions = []
    seen = set()

    for suffix in suffixes:
        query = urllib.parse.quote(seed_keyword + suffix)
        url = (
            f"https://suggestqueries.google.com/complete/search"
            f"?client=youtube&ds=yt&q={query}"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as response:
                raw = response.read().decode("utf-8")
                import json as _json
                data = _json.loads(raw)
                terms = [item[0] for item in data[1] if isinstance(item, list) and item]
                for term in terms:
                    if term not in seen:
                        seen.add(term)
                        suggestions.append(term)
        except Exception as e:
            logger.warning("Keyword fetch failed for suffix '%s': %s", suffix, e)
            continue

    top_suggestions = suggestions[:30]

    if not top_suggestions:
        return (
            f"No trending keywords found for '{seed_keyword}'.\n"
            "Possible reason: network timeout or YouTube blocked the request.\n"
            "Proceed with metadata generation using the title and SOP rules."
        )

    formatted = "\n".join(f"  {i+1:02d}. {s}" for i, s in enumerate(top_suggestions))
    sep = "═" * 60

    return (
        f"{sep}\n"
        f"TRENDING KEYWORDS for: '{seed_keyword}'\n"
        f"{sep}\n"
        f"{formatted}\n"
        f"{sep}\n\n"
        "INSTRUCTION FOR CLAUDE:\n"
        "  • Pick 2-3 of the most relevant terms above.\n"
        "  • Weave them naturally into the title (first 40 chars) and\n"
        "    the first 500 chars of the description.\n"
        "  • Add the most relevant ones as tags (respecting SOP tag rules).\n"
        "  • Do NOT force irrelevant keywords in just because they ranked.\n"
        "  • These are real searches real people are making right now."
    )


@mcp.tool()
def analyze_competitor_video(video_url: str) -> str:
    """
    Fetch and analyze metadata from a top-ranking or competitor YouTube video.

    Use this to benchmark what is working for high-performing videos in the
    same niche. Pass in the URL of a video that ranks well for your target
    keyword. Claude will compare its generated metadata against this benchmark.

    This tool is READ-ONLY. It does not modify any video.

    Args:
        video_url: Full YouTube URL or bare 11-character video ID of any
                   publicly accessible video to analyze.

    Returns:
        Structured analysis: title length, tag count, description structure,
        whether chapters exist, opening line, and a calibration instruction.
    """
    video_id = extract_video_id(video_url)
    if not video_id:
        return (
            f"ERROR: Could not extract a YouTube video ID from: '{video_url}'\n"
            "Accepted formats: youtube.com/watch?v=ID, youtu.be/ID, "
            "youtube.com/shorts/ID, or a bare 11-character video ID."
        )

    logger.info("analyze_competitor_video: %s", video_id)

    try:
        data = get_video_data(video_id)
    except ValueError as e:
        return f"ERROR (Video not found): {e}"
    except RuntimeError as e:
        return f"ERROR (YouTube API): {e}"

    description = data.get("description", "")
    tags = data.get("tags", [])
    title = data.get("title", "")

    has_chapters = "00:00" in description

    desc_opening = description[:200].strip()
    if len(description) > 200:
        desc_opening += "..."

    tags_display = ", ".join(tags[:10])
    if len(tags) > 10:
        tags_display += f" ... (+{len(tags) - 10} more)"

    try:
        views_fmt = f"{int(data['view_count']):,}"
    except (ValueError, TypeError):
        views_fmt = data.get("view_count", "N/A")

    sep = "═" * 60
    thin = "─" * 60

    return (
        f"{sep}\n"
        f"COMPETITOR VIDEO ANALYSIS\n"
        f"{sep}\n"
        f"URL        : https://youtube.com/watch?v={video_id}\n"
        f"Views      : {views_fmt}\n"
        f"Likes      : {data.get('like_count', 'N/A')}\n"
        f"Comments   : {data.get('comment_count', 'N/A')}\n"
        f"{thin}\n"
        f"TITLE ({len(title)} chars):\n"
        f"  {title}\n"
        f"{thin}\n"
        f"TAGS ({len(tags)} total):\n"
        f"  First 3 (highest weight): {', '.join(tags[:3]) if tags else 'none'}\n"
        f"  All visible: {tags_display}\n"
        f"{thin}\n"
        f"DESCRIPTION ({len(description)} chars):\n"
        f"  Has chapters (00:00): {'YES — boosts search indexing' if has_chapters else 'NO'}\n"
        f"  Opening (first 200 chars):\n"
        f"    {desc_opening}\n"
        f"{sep}\n\n"
        "CALIBRATION INSTRUCTION FOR CLAUDE:\n"
        "  • Your generated title should be comparable in length and keyword placement.\n"
        "  • Match or exceed their tag count (within SOP limits of 15-20).\n"
        "  • If they have chapters and your video is long-form, you must also add chapters.\n"
        "  • Your description opening must be as strong or stronger than theirs.\n"
        "  • Do NOT copy their title, tags, or description — use this for structure only."
    )


@mcp.tool()
def fetch_transcript(video_url: str) -> str:
    """
    Fetch and translate the transcript of a YouTube video for metadata context.

    Behavior differs based on video duration:

    LONG-FORM (> 60 seconds):
        Fetches Hindi auto-captions and translates them to English.
        Returns the translated transcript as full story context.
        Claude should use this to write accurate, specific metadata.

    SHORTS (<= 60 seconds):
        This channel's Shorts use BORROWED AUDIO from other creators
        (Bollywood songs, viral sounds, trending dialogue).
        The transcript = that borrowed audio, NOT Punji's visual story.
        Tool still fetches the transcript but labels it as "audio flavour only".
        Claude must apply the 80/20 rule:
            80% metadata from the title (Mom's creative intent)
            20% emotional flavour extracted from the audio transcript

    In both cases, if no transcript is available, returns a clear message
    instructing Claude to generate from title only.

    Args:
        video_url: YouTube video URL (any format) or bare 11-character video ID.

    Returns:
        Translated transcript with clear labelling and instructions for Claude,
        or a fallback message if transcript is unavailable.
    """
    try:
        from youtube_transcript_api import (
            YouTubeTranscriptApi,
            NoTranscriptFound,
            TranscriptsDisabled,
        )
    except ImportError:
        return (
            "ERROR: youtube-transcript-api is not installed.\n"
            "Run: pip install youtube-transcript-api\n"
            "Then restart the MCP server."
        )

    video_id = extract_video_id(video_url)
    if not video_id:
        return (
            f"ERROR: Could not extract a YouTube video ID from: '{video_url}'\n"
            "Accepted formats: youtube.com/watch?v=ID, youtu.be/ID, "
            "youtube.com/shorts/ID, or a bare 11-character video ID."
        )

    logger.info("fetch_transcript: %s", video_id)

    try:
        data = get_video_data(video_id)
    except Exception as e:
        return f"ERROR: Could not fetch video data to check duration: {e}"

    duration_iso = data.get("duration", "")
    video_title = data.get("title", "")

    total_seconds = 0
    h_match = re.search(r"(\d+)H", duration_iso)
    m_match = re.search(r"(\d+)M", duration_iso)
    s_match = re.search(r"(\d+)S", duration_iso)
    if h_match:
        total_seconds += int(h_match.group(1)) * 3600
    if m_match:
        total_seconds += int(m_match.group(1)) * 60
    if s_match:
        total_seconds += int(s_match.group(1))

    is_short = 0 < total_seconds <= 60

    sep = "═" * 60
    thin = "─" * 60

    transcript_text = None
    transcript_source = None

    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        try:
            transcript_obj = transcript_list.find_transcript(["hi"])
            try:
                translated = transcript_obj.translate("en")
                segments = translated.fetch()
                transcript_source = "Hindi → English (auto-translated)"
            except Exception:
                segments = transcript_obj.fetch()
                transcript_source = "Hindi (raw — translation unavailable)"
        except NoTranscriptFound:
            try:
                transcript_obj = transcript_list.find_generated_transcript(["hi", "en", "hi-IN"])
                segments = transcript_obj.fetch()
                transcript_source = f"Auto-generated ({transcript_obj.language})"
            except Exception:
                segments = None

        if segments:
            raw_text = " ".join(
                seg["text"] for seg in segments
                if isinstance(seg, dict) and seg.get("text")
            )
            raw_text = re.sub(r"\[.*?\]", "", raw_text)
            raw_text = re.sub(r"\s+", " ", raw_text).strip()
            transcript_text = raw_text

    except TranscriptsDisabled:
        transcript_text = None
        transcript_source = "DISABLED"
    except NoTranscriptFound:
        transcript_text = None
        transcript_source = "NOT_FOUND"
    except Exception as e:
        logger.warning("Transcript fetch failed for %s: %s", video_id, e)
        transcript_text = None
        transcript_source = f"ERROR: {e}"

    if is_short:
        response_parts = [
            sep,
            "SHORTS DETECTED",
            sep,
            f"Video ID  : {video_id}",
            f"Duration  : {total_seconds}s (≤ 60s = YouTube Short)",
            f"Title (Mom's intent — 80% weight):",
            f"  {video_title}",
            thin,
        ]

        if transcript_text:
            flavour = transcript_text[:800]
            if len(transcript_text) > 800:
                flavour += "... [trimmed]"

            response_parts += [
                f"BORROWED AUDIO TRANSCRIPT — FLAVOUR ONLY (20% weight):",
                f"Source: {transcript_source}",
                thin,
                flavour,
                thin,
                "",
                "INSTRUCTION FOR CLAUDE — READ CAREFULLY:",
                "  This Short uses borrowed audio from another YouTube creator.",
                "  The transcript above is that creator's audio, NOT Punji's story.",
                "",
                "  DO NOT:",
                "  • Treat the transcript as what the video is about",
                "  • Copy any audio lyrics or dialogue into the description",
                "  • Use audio character names, places, or plot in metadata",
                "",
                "  DO:",
                "  • Extract the MOOD and EMOTION from the audio",
                "  • Apply that mood as a 20% flavour to the metadata tone",
                "  • Build 80% of metadata from the title (Mom's intent above)",
                "",
                "  HOW TO READ THE AUDIO FLAVOUR:",
                "  • Sad/longing song → weave loyalty, waiting, unconditional love into description",
                "  • Energetic/hype audio → make title and description feel exciting, fast-paced",
                "  • Funny/comedic dialogue → keep tone light, playful, humorous",
                "  • Devotional/peaceful song → use calm, warm, heartwarming tone",
                "  • Romantic song → weave themes of loyalty, bond, love between Punji and family",
                "",
                "  EXAMPLE:",
                "  Title: 'Punji waiting at the door'",
                "  Audio: sad longing Bollywood song about missing someone",
                "  Result: description tone = longing, loyalty, 'waiting for the one person...'",
                "          NOT a description of the song's story",
                sep,
            ]
        else:
            no_transcript_reason = {
                "DISABLED": "Transcripts are disabled for this video.",
                "NOT_FOUND": "No transcript found — video may not have auto-captions yet.",
            }.get(transcript_source, f"Transcript unavailable: {transcript_source}")

            response_parts += [
                f"BORROWED AUDIO TRANSCRIPT: Not available",
                f"Reason: {no_transcript_reason}",
                thin,
                "INSTRUCTION FOR CLAUDE:",
                "  No audio transcript is available for this Short.",
                "  Generate metadata from the title only (100% title-driven).",
                "  Apply standard SOP + SEO rules.",
                sep,
            ]

        return "\n".join(response_parts)

    else:
        response_parts = [
            sep,
            "LONG-FORM VIDEO TRANSCRIPT",
            sep,
            f"Video ID : {video_id}",
            f"Duration : {duration_iso} ({total_seconds}s)",
            f"Title    : {video_title}",
            thin,
        ]

        if transcript_text:
            trimmed = transcript_text[:2000]
            if len(transcript_text) > 2000:
                trimmed += "\n... [transcript trimmed at 2000 chars for context budget]"

            response_parts += [
                f"TRANSCRIPT ({transcript_source}):",
                thin,
                trimmed,
                thin,
                "",
                "INSTRUCTION FOR CLAUDE:",
                "  • Use this transcript as the primary story context for metadata.",
                "  • The transcript reflects what actually happens in the video.",
                "  • Do NOT copy transcript lines verbatim into the description.",
                "  • Rephrase naturally — write as if you witnessed the moment.",
                "  • Extract: key events, emotional moments, people/dogs mentioned,",
                "    locations, and any specific activities or firsts.",
                "  • Use these details to write a specific, accurate description",
                "    (not generic filler about 'Punji's day').",
                sep,
            ]
        else:
            no_transcript_reason = {
                "DISABLED": "Transcripts are disabled for this video.",
                "NOT_FOUND": "No transcript found — video may not have auto-captions yet.",
            }.get(transcript_source, f"Transcript unavailable: {transcript_source}")

            response_parts += [
                "TRANSCRIPT: Not available",
                f"Reason: {no_transcript_reason}",
                thin,
                "INSTRUCTION FOR CLAUDE:",
                "  No transcript is available for this video.",
                "  Generate metadata from the video title only.",
                "  Apply the Content Inference Rule from the SOP:",
                "  infer scene, emotion, story arc, and keywords from the title alone.",
                sep,
            ]

        return "\n".join(response_parts)


@mcp.tool()
def authenticate_youtube() -> str:
    """
    Run the one-time OAuth 2.0 browser authentication flow for YouTube write access.

    Opens a browser window for the channel owner to sign in and grant permission.
    Saves the token to token.json in the project directory for all future sessions.
    Must be run once before update_video_metadata can be used.

    client_secrets.json must exist in the project directory before calling this.
    Download it from Google Cloud Console → APIs & Services → Credentials.

    Returns:
        Success message confirming authentication completed, or error with instructions.
    """
    if not os.path.exists(CLIENT_SECRETS_PATH):
        return (
            "ERROR: client_secrets.json not found.\n\n"
            "To get it:\n"
            "1. Go to https://console.cloud.google.com\n"
            "2. Select your project (or create one)\n"
            "3. Enable 'YouTube Data API v3'\n"
            "4. Go to APIs & Services → Credentials\n"
            "5. Create OAuth 2.0 Client ID → Desktop App\n"
            f"6. Download and save as: {CLIENT_SECRETS_PATH}"
        )

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            CLIENT_SECRETS_PATH,
            scopes=YOUTUBE_SCOPES,
        )
        creds = flow.run_local_server(port=0, open_browser=True)
        _save_token(creds)
        logger.info("OAuth authentication successful. Token saved to %s", TOKEN_PATH)
        return (
            "YouTube authentication successful!\n"
            f"Token saved to: {TOKEN_PATH}\n\n"
            "You can now use update_video_metadata to update video titles, descriptions, and tags."
        )
    except Exception as e:
        logger.error("OAuth flow failed: %s", e)
        return f"ERROR: OAuth authentication failed: {e}"


@mcp.tool()
def update_video_metadata(
    video_id: str,
    title: str,
    description: str,
    tags: list[str],
) -> str:
    """
    Validate new metadata against the channel SOP and return a confirmation token.

    This tool does NOT write to YouTube. It validates the proposed title,
    description, and tags against all SOP hard constraints. If validation
    passes, it returns a CONFIRMATION TOKEN that must be passed to
    confirm_metadata_update to execute the actual write.

    This two-step pattern guarantees metadata is NEVER auto-applied without
    explicit human confirmation.

    Args:
        video_id    : The 11-character YouTube video ID (NOT a full URL).
                      Use fetch_video_data first to get the video ID.
        title       : New title (60–70 chars, Hindi word, emoji, no ALL CAPS).
        description : New description (1,200–3,500 chars, 5 sections).
        tags        : List of 15–20 tags. Must include 'punji the labrador'.

    Returns:
        PENDING CONFIRMATION message with token, or validation error list.
    """
    # Validate all fields against SOP
    violations = validate_metadata(title, description, tags)

    if violations:
        violation_list = "\n".join(f"  {i+1}. {v}" for i, v in enumerate(violations))
        _video_log(
            video_id,
            f"VALIDATION FAILED ({len(violations)} violations)",
            " | ".join(violations[:3]) + ("..." if len(violations) > 3 else ""),
        )
        return (
            f"VALIDATION FAILED — {len(violations)} violation(s) found.\n"
            "Fix all violations and call update_video_metadata again.\n\n"
            f"Violations:\n{violation_list}\n\n"
            "No changes have been made to YouTube."
        )

    # Generate a short confirmation token
    token = secrets.token_hex(4)
    _video_log(
        video_id,
        "VALIDATION PASSED — awaiting confirmation",
        f"Token: {token} | Title: {title} | Desc: {len(description)} chars | Tags: {len(tags)}",
    )  # 8-character hex string, e.g. "a3f7b21c"

    # Store pending update (in-memory, expires after TTL)
    _pending_updates[token] = {
        "video_id": video_id,
        "title": title,
        "description": description,
        "tags": tags,
        "created_at": datetime.now(timezone.utc).timestamp(),
    }

    sep = "─" * 60
    tags_preview = ", ".join(tags[:5]) + (f" ... (+{len(tags)-5} more)" if len(tags) > 5 else "")

    return (
        f"VALIDATION PASSED ✓\n\n"
        f"PENDING CONFIRMATION — review what will be applied:\n"
        f"{sep}\n"
        f"Video ID    : {video_id}\n"
        f"Video URL   : https://youtube.com/watch?v={video_id}\n"
        f"{sep}\n"
        f"NEW TITLE ({len(title)} chars):\n"
        f"  {title}\n\n"
        f"NEW DESCRIPTION ({len(description)} chars):\n"
        f"  {description[:200]}{'...' if len(description) > 200 else ''}\n\n"
        f"NEW TAGS ({len(tags)} tags):\n"
        f"  {tags_preview}\n"
        f"{sep}\n\n"
        f"To apply these changes to YouTube, call:\n"
        f'  confirm_metadata_update("{token}")\n\n'
        f"Token expires in {CONFIRMATION_TOKEN_TTL_SECONDS // 60} minutes.\n"
        "If you do NOT want to apply these changes, simply do nothing — the token will expire."
    )


@mcp.tool()
def update_tags_only(video_id: str, tags: list[str]) -> str:
    """
    Validate and stage a tags-only update for a video that already has a description.

    Fetches the existing title and description from YouTube and keeps them
    exactly as-is. Only the tags are changed. Validates tags against SOP rules
    and returns a confirmation token — does NOT write to YouTube yet.

    Args:
        video_id : The 11-character YouTube video ID.
        tags     : List of 15–20 tags. Must include 'punji the labrador'.

    Returns:
        PENDING CONFIRMATION message with token, or tag validation errors.
    """
    # Validate tags only
    violations: list[str] = []
    n_tags = len(tags)
    if n_tags < MIN_TAGS:
        violations.append(f"Only {n_tags} tags — minimum is {MIN_TAGS}.")
    elif n_tags > MAX_TAGS:
        violations.append(f"{n_tags} tags — maximum is {MAX_TAGS}.")

    tags_lower = [t.lower().strip() for t in tags]
    if REQUIRED_TAG not in tags_lower:
        violations.append(f"Tags must always include '{REQUIRED_TAG}'.")

    for tag in tags:
        if len(tag) > MAX_TAG_LENGTH:
            violations.append(f"Tag '{tag}' is {len(tag)} chars — max is {MAX_TAG_LENGTH}.")

    seen: set[str] = set()
    for tag in tags_lower:
        if tag in seen:
            violations.append(f"Duplicate tag: '{tag}'")
            break
        seen.add(tag)

    if violations:
        _video_log(video_id, f"TAGS VALIDATION FAILED ({len(violations)})", " | ".join(violations))
        return (
            f"TAG VALIDATION FAILED — {len(violations)} violation(s):\n"
            + "\n".join(f"  {i+1}. {v}" for i, v in enumerate(violations))
            + "\n\nFix and call update_tags_only again."
        )

    # Fetch existing title + description to preserve them
    try:
        current = get_video_data(video_id)
    except Exception as e:
        return f"ERROR: Could not fetch current video data: {e}"

    title = current["title"]
    description = current["description"]

    token = secrets.token_hex(4)
    _pending_updates[token] = {
        "video_id": video_id,
        "title": title,
        "description": description,
        "tags": tags,
        "created_at": datetime.now(timezone.utc).timestamp(),
    }
    _video_log(
        video_id,
        "TAGS VALIDATION PASSED — awaiting confirmation",
        f"Token: {token} | Tags: {len(tags)} | Existing title/desc preserved",
    )

    sep = "─" * 60
    tags_preview = ", ".join(tags[:5]) + (f" ... (+{len(tags)-5} more)" if len(tags) > 5 else "")

    return (
        f"TAG VALIDATION PASSED ✓\n\n"
        f"PENDING CONFIRMATION — tags only update:\n"
        f"{sep}\n"
        f"Video ID    : {video_id}\n"
        f"Video URL   : https://youtube.com/watch?v={video_id}\n"
        f"Title       : {title} (unchanged)\n"
        f"Description : {len(description)} chars (unchanged)\n"
        f"{sep}\n"
        f"NEW TAGS ({len(tags)} tags):\n"
        f"  {tags_preview}\n"
        f"{sep}\n\n"
        f"To apply, call:\n"
        f'  confirm_metadata_update("{token}")\n\n'
        f"Token expires in {CONFIRMATION_TOKEN_TTL_SECONDS // 60} minutes."
    )


@mcp.tool()
def confirm_metadata_update(confirmation_token: str) -> str:
    """
    Execute a confirmed metadata update on YouTube.

    Looks up the confirmation token returned by update_video_metadata and
    applies the validated title, description, and tags to the YouTube video
    via the YouTube Data API v3 (OAuth 2.0). Logs every attempt to
    .claude/UPDATE_LOG.md whether it succeeds or fails.

    Tokens expire 5 minutes after being issued by update_video_metadata.

    Args:
        confirmation_token: The 8-character token returned by update_video_metadata.

    Returns:
        Success message with the YouTube video URL, or a detailed error message.
    """
    # Look up token
    pending = _pending_updates.get(confirmation_token)
    if not pending:
        return (
            f"ERROR: Confirmation token '{confirmation_token}' not found.\n\n"
            "Possible reasons:\n"
            "  • Token has already been used\n"
            "  • Token expired (tokens expire after 5 minutes)\n"
            "  • Token was mistyped\n\n"
            "Call update_video_metadata again to generate a new token."
        )

    # Check TTL
    age = datetime.now(timezone.utc).timestamp() - pending["created_at"]
    if age > CONFIRMATION_TOKEN_TTL_SECONDS:
        del _pending_updates[confirmation_token]
        return (
            f"ERROR: Confirmation token '{confirmation_token}' has expired "
            f"({int(age)}s old — TTL is {CONFIRMATION_TOKEN_TTL_SECONDS}s).\n\n"
            "Call update_video_metadata again to generate a fresh token."
        )

    video_id = pending["video_id"]
    title = pending["title"]
    description = pending["description"]
    tags = pending["tags"]

    # Consume token immediately (one-use)
    del _pending_updates[confirmation_token]

    # ── Get OAuth credentials ──────────────────────────────────────────────
    try:
        creds = get_oauth_credentials()
    except FileNotFoundError as e:
        _log_update(video_id, "", title, description, tags, f"FAILED: {e}")
        return f"ERROR: {e}"
    except RuntimeError as e:
        _log_update(video_id, "", title, description, tags, f"FAILED: {e}")
        return f"ERROR: {e}"

    # ── Fetch current category_id (required field for videos.update) ───────
    try:
        current_data = get_video_data(video_id)
        category_id = current_data.get("category_id", "22")
        original_title = current_data.get("title", "")
    except Exception as e:
        _log_update(video_id, "", title, description, tags, f"FAILED (pre-fetch): {e}")
        return f"ERROR: Could not fetch current video data before updating: {e}"

    # ── Execute YouTube videos.update ──────────────────────────────────────
    try:
        youtube_auth = build("youtube", "v3", credentials=creds)
        response = youtube_auth.videos().update(
            part="snippet",
            body={
                "id": video_id,
                "snippet": {
                    "title": title,
                    "description": description,
                    "tags": tags,
                    "categoryId": category_id,
                },
            },
        ).execute()

        updated_title = response.get("snippet", {}).get("title", title)
        status_msg = "SUCCESS"
        _log_update(video_id, original_title, updated_title, description, tags, status_msg)
        logger.info("Metadata updated successfully for video %s", video_id)

        return (
            f"SUCCESS — Metadata updated on YouTube!\n\n"
            f"Video URL : https://youtube.com/watch?v={video_id}\n"
            f"Title     : {updated_title}\n"
            f"Description: {len(description)} chars applied\n"
            f"Tags      : {len(tags)} tags applied\n\n"
            "Update logged to .claude/UPDATE_LOG.md"
        )

    except HttpError as e:
        status = e.resp.status
        error_msg = f"YouTube API HTTP {status}: {e}"
        _log_update(video_id, original_title, title, description, tags, f"FAILED: {error_msg}")
        logger.error("videos.update failed for %s: %s", video_id, error_msg)

        if status == 403:
            return (
                f"ERROR: Permission denied (HTTP 403).\n\n"
                "Possible causes:\n"
                "  • The OAuth account does not own this video\n"
                "  • The OAuth token does not have youtube.force-ssl scope\n"
                "  • Re-run authenticate_youtube to refresh permissions\n\n"
                f"Raw error: {e}"
            )
        if status == 401:
            return (
                "ERROR: OAuth token is invalid or revoked (HTTP 401).\n"
                "Run authenticate_youtube again to re-authenticate.\n\n"
                f"Raw error: {e}"
            )
        return f"ERROR: YouTube API update failed (HTTP {status}): {e}"

    except Exception as e:
        error_msg = str(e)
        _log_update(video_id, original_title, title, description, tags, f"FAILED: {error_msg}")
        logger.error("Unexpected error updating %s: %s", video_id, error_msg)
        return f"ERROR: Unexpected error during YouTube update: {e}"


@mcp.tool()
def load_channel_queue(count: int = 10) -> str:
    """
    Fetch the latest N videos from the Punji the Labrador channel and save them
    as a processing queue. Call this once, then call fetch_next_video repeatedly
    to process each video one by one.

    Videos that already have a description will cause the queue to stop when
    reached — they are never modified.

    Args:
        count: Number of recent videos to load into the queue (default 10, max 50).

    Returns:
        List of queued videos with their titles and IDs.
    """
    count = max(1, min(count, 50))

    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        return "ERROR: YOUTUBE_API_KEY is not set."

    try:
        uploads_playlist_id = _get_uploads_playlist_id(api_key)
        youtube = build("youtube", "v3", developerKey=api_key)
        response = youtube.playlistItems().list(
            playlistId=uploads_playlist_id,
            part="snippet",
            maxResults=count,
        ).execute()
    except Exception as e:
        return f"ERROR: Could not fetch channel videos: {e}"

    items = response.get("items", [])
    if not items:
        return "ERROR: No videos found on the channel."

    video_ids = [
        item["snippet"]["resourceId"]["videoId"]
        for item in items
        if item["snippet"]["resourceId"].get("kind") == "youtube#video"
    ]

    queue = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total": len(video_ids),
        "processed": 0,
        "remaining": video_ids,
    }
    _save_queue(queue)

    # Build a readable list with titles
    lines = [
        f"QUEUE LOADED — {len(video_ids)} videos ready to process",
        f"Channel: @{CHANNEL_HANDLE}",
        "─" * 50,
    ]
    for i, item in enumerate(items[:len(video_ids)], 1):
        vid_id = item["snippet"]["resourceId"]["videoId"]
        title = item["snippet"].get("title", "Unknown")
        lines.append(f"  {i:2}. [{vid_id}] {title}")

    lines += [
        "─" * 50,
        "Now call fetch_next_video to start processing one by one.",
        "Videos that already have a description will stop the queue automatically.",
    ]
    return "\n".join(lines)


@mcp.tool()
def fetch_next_video() -> str:
    """
    Fetch the next video from the queue loaded by load_channel_queue.

    Checks if the video already has a description — if it does, the queue
    stops immediately and the video is NOT processed.

    Returns the video data and SOP rules so Claude can generate new metadata,
    or a STOP/DONE message if the queue is empty or a video already has a description.
    """
    queue = _load_queue()

    if not queue:
        return (
            "NO QUEUE FOUND.\n"
            "Call load_channel_queue first to fetch videos from the channel."
        )

    if not queue["remaining"]:
        return (
            f"QUEUE COMPLETE — all {queue['total']} videos have been processed.\n"
            "Call load_channel_queue again to start a new batch."
        )

    # ── Find next video — skip only if description AND tags both exist ───────
    while queue["remaining"]:
        video_id = queue["remaining"][0]

        try:
            data = get_video_data(video_id)
        except Exception as e:
            _video_log(video_id, "FETCH FAILED (queue)", str(e))
            queue["remaining"].pop(0)
            queue["processed"] += 1
            _save_queue(queue)
            continue

        has_description = len(data["description"].strip()) > EXISTING_DESCRIPTION_THRESHOLD
        has_tags = len(data["tags"]) >= MIN_TAGS

        if has_description and has_tags:
            # Fully complete — skip it
            _video_log(
                video_id,
                "SKIPPED — already has description and tags",
                f"Desc: {len(data['description'])} chars | Tags: {len(data['tags'])}",
            )
            queue["remaining"].pop(0)
            queue["processed"] += 1
            _save_queue(queue)
            continue

        # Needs work — pop and process
        queue["remaining"].pop(0)
        queue["processed"] += 1
        _save_queue(queue)
        break

    else:
        return (
            f"QUEUE COMPLETE — all {queue['total']} videos checked.\n"
            "Every video already has a description and tags.\n"
            "Call load_channel_queue again to start a new batch."
        )

    # Determine what this video needs
    has_description = len(data["description"].strip()) > EXISTING_DESCRIPTION_THRESHOLD
    has_tags = len(data["tags"]) >= MIN_TAGS

    if not has_description and not has_tags:
        action = "GENERATE FULL METADATA (title + description + tags)"
        instruction = "Use update_video_metadata with the new title, description, and tags."
    else:
        action = "GENERATE TAGS ONLY (description already exists — do NOT modify it)"
        instruction = (
            "Use update_tags_only with only the new tags list.\n"
            "The existing title and description will be preserved exactly as-is."
        )

    _video_log(
        video_id,
        f"QUEUE — fetched ({queue['processed']}/{queue['total']}) — {action}",
        f"Title: {data['title']} | Desc: {len(data['description'])} chars | Tags: {len(data['tags'])}",
    )

    tags_display = (
        "\n".join(f"  {i+1}. {t}" for i, t in enumerate(data["tags"]))
        if data["tags"] else "  (no tags currently set)"
    )

    def _fmt_stat(label: str, val: str) -> str:
        if val == "N/A":
            return f"{label}: N/A"
        try:
            return f"{label}: {int(val):,}"
        except (ValueError, TypeError):
            return f"{label}: {val}"

    sep = "═" * 60
    progress = f"[{queue['processed']}/{queue['total']}]"

    lines = [
        sep,
        f"NEXT VIDEO {progress} — https://youtube.com/watch?v={video_id}",
        f"ACTION REQUIRED: {action}",
        sep,
        f"Title        : {data['title']}",
        f"Published    : {data['published_at'][:10] if data['published_at'] else 'unknown'}",
        f"Duration     : {data['duration']}",
        _fmt_stat("Views        ", data["view_count"]),
        _fmt_stat("Likes        ", data["like_count"]),
        _fmt_stat("Comments     ", data["comment_count"]),
        "",
        sep,
        f"CURRENT DESCRIPTION ({len(data['description'])} chars):",
        sep,
        data["description"] if data["description"].strip() else "(empty — needs generating)",
        "",
        sep,
        f"CURRENT TAGS ({len(data['tags'])} tags):",
        sep,
        tags_display,
        "",
        sep,
        f"VIDEO ID : {video_id}",
        f"Remaining: {len(queue['remaining'])} videos after this",
        f"INSTRUCTION: {instruction}",
        sep,
        CHANNEL_SOP,
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
