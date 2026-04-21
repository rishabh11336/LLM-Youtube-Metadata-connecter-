# Setup Guide — Punji the Labrador YouTube Metadata MCP Server

This server runs inside **Claude Desktop** and gives you four tools:

| Tool | What it does |
|---|---|
| `fetch_video_data` | Fetch current video title, description, tags, stats from YouTube |
| `authenticate_youtube` | One-time OAuth login so the server can write to YouTube |
| `update_video_metadata` | Validate new metadata + get a confirmation token |
| `confirm_metadata_update` | Apply the metadata to YouTube (only after token confirmed) |

---

## Prerequisites

- **Python 3.10+** (this project uses 3.13)
  - Check: `python3 --version`
- **Claude Desktop** installed (claude.ai/download)
- **Google Cloud account** (free)

---

## Step 1 — Get a YouTube Data API v3 Key (for reading)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (e.g. "Punji MCP")
3. In the left menu → **APIs & Services → Library**
4. Search for **YouTube Data API v3** → Enable it
5. Go to **APIs & Services → Credentials**
6. Click **+ Create Credentials → API Key**
7. Copy the key — you'll need it in Step 4

---

## Step 2 — Create OAuth 2.0 Credentials (for writing)

These allow the server to update video metadata on behalf of the channel owner.

1. Same **Credentials** page → click **+ Create Credentials → OAuth 2.0 Client ID**
2. If prompted, configure the **OAuth consent screen**:
   - User type: **External** (or Internal if you have Google Workspace)
   - App name: `Punji MCP` (anything)
   - Add your email as test user
   - Scopes: add `https://www.googleapis.com/auth/youtube.force-ssl`
3. Application type: **Desktop app**
4. Name: `Punji MCP Desktop`
5. Click **Create**
6. Click **Download JSON**
7. **Rename the downloaded file to `client_secrets.json`**
8. **Place it in the project root:**
   ```
   <project-root>/client_secrets.json
   ```

> ⚠️ `client_secrets.json` is in `.gitignore` — it will never be committed.

---

## Step 3 — Install Python Dependencies

```bash
cd "<project-root>"
pip3 install -r requirements.txt
```

---

## Step 4 — Configure Your YouTube API Key

Create a `.env` file in the project root:

```bash
cd "<project-root>"
cp .env.example .env
```

Open `.env` and replace `your_youtube_data_api_v3_key_here` with your real key from Step 1.

> Alternatively, you can put the key directly in the Claude Desktop config (Step 5).

---

## Step 5 — Add the Server to Claude Desktop

1. Open the Claude Desktop config file:
   ```
   ~/Library/Application Support/Claude/claude_desktop_config.json
   ```

2. Add the `punji-metadata` block into the existing `mcpServers` object:
   ```json
   {
     "mcpServers": {
       "punji-metadata": {
         "command": "python3",
         "args": [
           "/absolute/path/to/server.py"
         ],
         "env": {
           "YOUTUBE_API_KEY": "your_api_key_here"
         }
       }
     }
   }
   ```
   The full snippet is also in `claude_desktop_config_snippet.json`.

3. **Fully quit Claude Desktop** (Cmd+Q — not just close the window) and reopen it.

---

## Step 6 — One-Time OAuth Authentication

The first time you want to use `update_video_metadata`, you must authenticate:

1. Open Claude Desktop
2. In a new conversation, type:
   > "Call authenticate_youtube"

3. A browser window will open → Sign in with the YouTube channel owner's Google account
4. Grant permission to manage YouTube videos
5. The browser will redirect to `localhost` and show a success page
6. Claude will confirm: `"YouTube authentication successful! Token saved to token.json"`

> This only needs to be done once. The token auto-refreshes silently after that.

---

## Step 7 — Verify It's Working

Test the read tool with any YouTube video:

> "Use fetch_video_data with https://www.youtube.com/watch?v=YOUR_VIDEO_ID"

You should see the current title, description, tags, views, likes, and the full SOP rules.

---

## Full Example Conversation Flow

```
You:     fetch_video_data("https://youtube.com/watch?v=ABC123xyz01")

Claude:  [calls fetch_video_data, returns current video data + SOP rules]
         Title: Punji ka din — labrador aur uske dost
         Views: 12,345
         ...

You:     Now generate optimised metadata for this video following the SOP.

Claude:  [generates title, description, tags from the fetched data]
         Title: Punji का दिन 🐾 — labrador ne street dog ko khana...  (65 chars)
         Description: [1,450 chars with all 5 sections]
         Tags: ["punji the labrador", "labrador", ...]

You:     Apply this metadata. Use update_video_metadata with video ID ABC123xyz01.

Claude:  [calls update_video_metadata, validates all fields]
         VALIDATION PASSED ✓
         PENDING CONFIRMATION
         Token: a3f7b21c
         Expires in 5 minutes.
         To apply: call confirm_metadata_update("a3f7b21c")

You:     Yes, apply it. confirm_metadata_update("a3f7b21c")

Claude:  [calls confirm_metadata_update, writes to YouTube]
         SUCCESS — Metadata updated on YouTube!
         https://youtube.com/watch?v=ABC123xyz01
         Update logged to .claude/UPDATE_LOG.md
```

---

## File Structure

```
MCP in Dev/
├── server.py                         ← Main MCP server (the only runtime file)
├── requirements.txt                  ← Python dependencies
├── .env                              ← Your API key (gitignored, create from .env.example)
├── .env.example                      ← Template
├── client_secrets.json               ← OAuth credentials (gitignored, download from GCloud)
├── client_secrets.json.example       ← Structure reference
├── token.json                        ← Auto-created after first authenticate_youtube (gitignored)
├── claude_desktop_config_snippet.json ← Config block to merge into Claude Desktop
├── SETUP.md                          ← This file
├── .gitignore                        ← Keeps secrets out of git
└── .claude/
    ├── PROGRESS.md                   ← Build log (read at start of every session)
    ├── DECISIONS.md                  ← Architecture decisions
    ├── ERRORS.md                     ← Error log
    └── UPDATE_LOG.md                 ← Every metadata write is logged here
```

---

## Troubleshooting

**"YOUTUBE_API_KEY environment variable is not set"**
→ Add the key to the `env` block in Claude Desktop config, then restart Claude Desktop.

**"client_secrets.json not found"**
→ Download from Google Cloud Console and place in the project root (see Step 2).

**"No valid OAuth token found. Call authenticate_youtube first"**
→ Run `authenticate_youtube` tool in Claude Desktop chat (see Step 6).

**"Permission denied (HTTP 403)"**
→ The OAuth account may not own the video, or the token is missing the required scope.
→ Re-run `authenticate_youtube` to get a fresh token with full scope.

**"YouTube API daily quota exceeded"**
→ The free quota is 10,000 units/day. Each `fetch_video_data` call costs ~3 units.
→ Quota resets at midnight Pacific Time.

**Server not appearing in Claude Desktop**
→ Make sure you fully quit (Cmd+Q) and restarted Claude Desktop.
→ Check the config JSON is valid (no trailing commas, correct braces).
