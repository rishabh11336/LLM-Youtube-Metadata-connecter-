<div align="center">

# 🐾 Claude Desktop YouTube Metadata Connector

**An MCP server that connects Claude Desktop to the YouTube Data API — fetch video data, generate SOP-compliant metadata with AI, and publish updates with a two-step confirmation flow.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple.svg)](https://modelcontextprotocol.io)
[![YouTube Data API v3](https://img.shields.io/badge/API-YouTube%20Data%20v3-red.svg)](https://developers.google.com/youtube/v3)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

---

## What It Does

This MCP (Model Context Protocol) server plugs directly into **Claude Desktop** and exposes four tools that let you manage YouTube video metadata through natural conversation:

| Tool | Description |
|------|-------------|
| `fetch_video_data` | Fetch current title, description, tags, views, likes, comments from YouTube |
| `authenticate_youtube` | One-time OAuth 2.0 browser flow to grant YouTube write access |
| `update_video_metadata` | Validate new metadata against channel SOP rules → return confirmation token |
| `confirm_metadata_update` | Execute the actual YouTube write only after explicit human confirmation |

### Two-Step Safety Architecture

Metadata is **never auto-applied**. Every update requires:
1. `update_video_metadata` validates the content and returns a short-lived token
2. `confirm_metadata_update(token)` triggers the actual YouTube API write

This ensures Claude cannot modify your videos without your explicit go-ahead.

---

## Demo Flow

```
You:    fetch_video_data("https://youtube.com/watch?v=ABC123xyz01")

Claude: [returns current title, description, tags, views, full SOP rules]

You:    Now generate optimised metadata following the SOP.

Claude: Title: Punji का दिन 🐾 — labrador aur uske pyaare dost  (64 chars)
        Description: [1,450 chars • 5 sections • Hindi+English]
        Tags: ["punji the labrador", "labrador", ...]

You:    Apply this. update_video_metadata("ABC123xyz01", title, description, tags)

Claude: VALIDATION PASSED ✓
        PENDING CONFIRMATION — Token: a3f7b21c (expires in 5 min)
        To apply: confirm_metadata_update("a3f7b21c")

You:    Yes — confirm_metadata_update("a3f7b21c")

Claude: SUCCESS — Metadata updated on YouTube!
        https://youtube.com/watch?v=ABC123xyz01
```

---

## Built-in SOP Validation

Every metadata submission is validated against a strict channel SOP before a token is issued:

**Title rules**
- 60–70 characters exactly
- Must contain at least one Hindi/Devanagari word
- Must contain exactly one emoji (not as the first character)
- No ALL CAPS words (3+ letters)
- Must not start with a number
- No clickbait phrases (e.g. "shocking", "you won't believe", "gone wrong")

**Description rules**
- 1,200–3,500 characters
- Must contain all 5 sections: Hook → Story → About → Socials → Hashtag block
- Must not copy the title verbatim

**Tag rules**
- 15–20 tags (not fewer, not more)
- `"punji the labrador"` must always be included
- No tag longer than 30 characters
- No duplicate tags

---

## Project Structure

```
.
├── server.py                         # MCP server — the only runtime file
├── requirements.txt                  # Python dependencies
├── .env.example                      # Template — copy to .env and add your API key
├── client_secrets.json.example       # OAuth credential structure reference
├── claude_desktop_config_snippet.json # Config block to merge into Claude Desktop
├── SETUP.md                          # Detailed step-by-step setup guide
├── .gitignore                        # Keeps .env, token.json, client_secrets.json out of git
└── .claude/
    ├── PROGRESS.md
    ├── DECISIONS.md
    ├── ERRORS.md
    └── UPDATE_LOG.md                 # Every metadata write is logged here
```

**Files never committed (gitignored):**
- `.env` — contains your YouTube API key
- `client_secrets.json` — contains your Google OAuth client secret
- `token.json` — auto-generated OAuth token

---

## Quick Start

### Prerequisites

- Python 3.10+ (`python3 --version`)
- [Claude Desktop](https://claude.ai/download)
- A [Google Cloud](https://console.cloud.google.com) account (free tier is fine)

### 1 · Clone the repo

```bash
git clone https://github.com/rishabh11336/Claude-Desktop-Youtube-Metadata-connecter-.git
cd Claude-Desktop-Youtube-Metadata-connecter-
```

### 2 · Install dependencies

```bash
pip3 install -r requirements.txt
```

### 3 · Set your YouTube API key

```bash
cp .env.example .env
# Open .env and replace the placeholder with your real YouTube Data API v3 key
```

> Get a key at [console.cloud.google.com](https://console.cloud.google.com) → APIs & Services → Credentials → Create API Key.
> Enable "YouTube Data API v3" in your project first.

### 4 · Add OAuth credentials

Download your OAuth 2.0 Desktop App credentials from Google Cloud Console, rename the file to `client_secrets.json`, and place it in the project root. See `client_secrets.json.example` for the expected structure.

### 5 · Register with Claude Desktop

Open `~/Library/Application Support/Claude/claude_desktop_config.json` and merge:

```json
{
  "mcpServers": {
    "punji-metadata": {
      "command": "python3",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "YOUTUBE_API_KEY": "YOUR_YOUTUBE_API_KEY_HERE"
      }
    }
  }
}
```

A ready-to-edit snippet is in `claude_desktop_config_snippet.json`.

**Fully quit Claude Desktop** (Cmd+Q) and reopen.

### 6 · One-time OAuth login

In a Claude Desktop chat:
> "Call authenticate_youtube"

A browser window will open. Sign in as the YouTube channel owner and grant access. The token is saved locally to `token.json` and auto-refreshes silently from then on.

For the full setup walkthrough see **[SETUP.md](SETUP.md)**.

---

## Security Notes

| File | Sensitivity | Protection |
|------|-------------|------------|
| `.env` | Contains YouTube API key | `.gitignore` |
| `client_secrets.json` | Contains Google OAuth secret | `.gitignore` |
| `token.json` | Contains OAuth access + refresh tokens | `.gitignore` |

> ⚠️ **Never commit these files.** They are excluded via `.gitignore`. If you accidentally expose a key, revoke it immediately in [Google Cloud Console](https://console.cloud.google.com).

---

## Requirements

```
mcp>=1.2.0
google-api-python-client>=2.0.0
google-auth-oauthlib>=1.0.0
google-auth-httplib2>=0.1.0
python-dotenv>=1.0.0
```

---

## License

MIT — see [LICENSE](LICENSE) for details.
