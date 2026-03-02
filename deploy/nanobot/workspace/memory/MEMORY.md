# Long-term Memory

This file stores important information that should persist across sessions.

## User Information

- **Language**: Chinese (简体中文)
- **Location**: Appears to be in China timezone (conversations at ~03:44-04:29 UTC)
- **Interests**: Tech, GitHub trending, AI agents, Vibe Coding, Xiaohongshu content

## Preferences

- Prefers Chinese language for communication
- Likes practical tech information (GitHub trending, coding trends)
- Interested in content creation on Xiaohongshu

## Project Context

1. **GitHub Trending 50**: Successfully fetched 50 hot GitHub repositories using REST API (web scraping of trending page failed due to JS rendering). File saved at `/root/.nanobot/workspace/github-trending-50.md`

2. **Xiaohongshu Task**:
   - Target article: "Vibe Coding开发前的 7 个关键步骤" by 鹿桃桃在Coding
   - Stats: 2058 likes, 3766收藏, 532分享, 42评论
   - Article has 21 images (1440x2400 vertical format)
   - URL: http://xhslink.com/o/92M3fjUwPyQ
   - **MCP Limitation Found**: `get_feed_detail` tool fails with "feed not found in noteDetailMap" error - can only retrieve cover images, not all 21 content images
   - Successfully retrieved and summarized article content (7 key steps for Vibe Coding)

3. **RPA Library Research**:
   - User wants RPA tools with HTTP API support (remote callable, not hardcoded scrapers)
   - Researched GitHub for Python RPA/automation libraries
   - File saved: `/root/.nanobot/workspace/rpa-libraries-research.md`
   - **Top 3 Recommendations**:
     - 🥇 **browser-use** (79,230 stars) - AI-driven, minimal code, easy to wrap with HTTP API
     - 🥈 **Skyvern** (20,579 stars) - Has built-in REST API, out-of-box remote control
     - 🥉 **Playwright** (83,226 stars) - Microsoft official, needs API wrapping

4. **MCP Tools Research** (from ClawHub search):
   - **browser-automation** (score 3.751) - Highest rated, browser control, no API key needed
   - **web-search** (3.651) / **ddg-web-search** (3.639) - Free web search, no API key
   - **browser-use** (1.238) - AI-driven browser automation
   - **git-essentials** (1.260) - Git operations
   - **sqlite** (0.989) - Local database, no config needed
   - **notion** (1.291) - Requires Notion Token
   - Most highly rated free MCPs don't require complex API key setup

## Xiaohongshu MCP Tools Available

- `check_login_status` - Check login status
- `get_login_qrcode` - Get login QR code
- `delete_cookies` - Logout
- `list_feeds` - Get home feed list
- `get_feed_detail` - Get note details (⚠️ has bugs, returns "feed not found" error)
- `search_feeds` - Search content
- `user_profile` - Get user profile
- `like_feed` - Like/unlike
- `favorite_feed` - Favorite/unfavorite
- `post_comment_to_feed` - Post comment
- `reply_comment_in_feed` - Reply to comment
- `publish_content` - Post image+text note
- `publish_with_video` - Post video note

## Important Notes

- GitHub Trending page uses JavaScript rendering - cannot be scraped directly, must use GitHub REST API
- nanobot (HKUDS/nanobot) ranked #3 on the trending list with 26,935 stars
- Xiaohongshu MCP has a bug - get_feed_detail fails intermittently, workaround is to use search_feeds first then get details
- User prefers RPA solutions with HTTP API capability for remote control
- **MCP browser-automation** is the top recommendation for free/easy browser control

---

*This file is automatically updated by nanobot when important information should be remembered.*