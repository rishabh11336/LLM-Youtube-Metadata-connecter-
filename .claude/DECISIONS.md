# DECISIONS — Architecture & Design Log

---

## D-001: No Anthropic API call in server (Apr 14 2026)
**Decision**: The MCP server does NOT call the Anthropic/Claude API. It only fetches YouTube data and returns it to Claude Desktop chat. Claude in the chat does the metadata generation.
**Reason**: When using Claude Desktop, the user is already talking to Claude. Calling Claude API again from the server is redundant, costs extra API credits, and adds unnecessary complexity. The server only needs one credential: YouTube API key (for reads) + OAuth (for writes).
**Impact**: Removes anthropic import, generate_with_claude, generate_with_retry from server.py. CHANNEL_CONTEXT and SOP_RULES are kept but only as context strings returned to Claude chat.

---

## D-002: Two-tool confirmation pattern for metadata updates (Apr 14 2026)
**Decision**: Metadata updates require two separate tool calls:
1. `update_video_metadata` → validates + returns a confirmation token (does NOT write)
2. `confirm_metadata_update(token)` → executes the actual YouTube API write
**Reason**: Claude must NEVER auto-apply metadata. The two-step pattern guarantees the user explicitly calls confirm. This prevents accidental writes if Claude misunderstands context.
**Impact**: In-memory `_pending_updates` dict stores token → {video_id, title, description, tags, created_at}. Tokens expire after 5 minutes (300 seconds).

---

## D-003: OAuth token stored in token.json in project root (Apr 14 2026)
**Decision**: OAuth2 credentials stored as `token.json` in the project root directory. `client_secrets.json` must also be in project root.
**Reason**: The server needs a predictable path to find credentials. Using the project root (same directory as server.py) is the simplest and most portable approach. Path is computed at runtime with `os.path.dirname(os.path.abspath(__file__))`.
**Impact**: Both files must be added to .gitignore. Never committed to version control.

---

## D-004: authenticate_youtube as a separate MCP tool (Apr 14 2026)
**Decision**: OAuth browser flow is exposed as a separate MCP tool `authenticate_youtube()` rather than auto-triggering on first use.
**Reason**: OAuth flow opens a browser and blocks. If it auto-triggers inside update_video_metadata on first call, the UX is confusing. Better to have the user explicitly call authenticate_youtube once during setup, then all subsequent tool calls work silently.
**Impact**: update_video_metadata and confirm_metadata_update return a clear error if token.json doesn't exist, directing the user to call authenticate_youtube first.

---

## D-005: fetch_video_data fetches statistics + snippet + contentDetails (Apr 14 2026)
**Decision**: `fetch_video_data` tool fetches part="snippet,statistics,contentDetails" to return view count, like count, comment count, published date, duration — not just title/description/tags.
**Reason**: Claude needs rich context to generate high-quality metadata. Views/likes/comments help Claude understand video performance. Published date helps understand video age.
**Impact**: Slightly higher API quota usage per call (one request, multiple parts).

---

## D-006: Updated SOP validation matches new production rules (Apr 14 2026)
**Decision**: validate_metadata is updated to enforce the full production SOP:
- Title: 60-70 chars, Hindi content required, emoji required (not at start), no ALL CAPS, no forbidden phrases, must not start with number
- Description: 1200-3500 chars
- Tags: 15-20 tags, "punji the labrador" always included, no tag over 30 chars, no duplicates
**Reason**: The old validate_metadata had looser rules that didn't match the actual channel SOP.

---

## D-007: category_id fetched at update time, not stored in confirmation token (Apr 14 2026)
**Decision**: When confirm_metadata_update executes the YouTube write, it fetches the current category_id from YouTube API (required field for videos.update). It does NOT rely on the user passing category_id.
**Reason**: YouTube videos.update requires categoryId in the snippet. The user shouldn't need to specify it — we preserve the existing category silently.
**Impact**: confirm_metadata_update makes one extra YouTube API read call (using API key, not OAuth) to get current category_id before writing.
