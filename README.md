# Metricool MCP Server

Remote [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) server for the [Metricool](https://metricool.com) social media management platform. It lets AI assistants like Claude schedule posts, query analytics, and manage brands on behalf of authenticated Metricool users.

**Endpoint:** `https://mcp.metricool.ai/mcp`
**Transport:** Streamable HTTP
**Authentication:** OAuth 2.0 (authorization code + PKCE)

---

## Features

| Capability | Tools |
|---|---|
| **Brand management** | Retrieve brand settings and connected social accounts |
| **Post scheduling** | Create and update scheduled posts across 10+ social networks |
| **Smart timing** | Get AI-powered best-time-to-post recommendations per network |
| **Analytics** | Browse available metrics and pull analytical data by date range |

### Supported social networks

Instagram, Facebook, X (Twitter), LinkedIn, TikTok, YouTube, Pinterest, Twitch, Bluesky, and Threads.

---

## Setup instructions

### Claude.ai (web)

1. Go to **Settings → Connectors** in [claude.ai](https://claude.ai).
2. Search for **Metricool** in the connector directory.
3. Click **Connect** — you will be redirected to Metricool to authorize access.
4. Once authorized, Metricool tools are available in your conversations.

### Claude Desktop

1. Open **Settings → MCP Servers**.
2. Add a new remote server with the URL:
   ```
   https://mcp.metricool.ai/mcp
   ```
3. Claude Desktop will open the OAuth flow in your browser. Log in to Metricool and authorize the connection.
4. The tools will appear in your tool list once connected.

### Claude Code (CLI)

```bash
claude mcp add metricool --transport streamable-http https://mcp.metricool.ai/mcp
```

Claude Code will prompt you to authenticate via OAuth the first time you use a Metricool tool.

---

## Authentication

This server uses **OAuth 2.0 with PKCE** (authorization code flow). No API keys are needed — users authenticate through their Metricool account.

### OAuth flow

1. The client discovers the authorization server via `/.well-known/oauth-protected-resource`.
2. The client redirects the user to `https://app.metricool.com/oauth/authorize`.
3. After the user logs in and grants permission, the authorization server issues a token.
4. The client includes the token as `Authorization: Bearer <token>` in all MCP requests.

### Scopes

| Scope | Description |
|---|---|
| `mcp:read` | Read brand settings, scheduled posts, and analytics |
| `mcp:write` | Create and update scheduled posts |

### Discovery endpoints

| Endpoint | Purpose |
|---|---|
| `GET /.well-known/oauth-protected-resource` | Protected Resource Metadata (RFC 8705) |
| `GET /.well-known/oauth-authorization-server` | Authorization Server Metadata (RFC 8414) |
| `GET /.well-known/openid-configuration` | OpenID Connect Discovery |

---

## Tools

### get_brand_settings

Retrieves the list of brands (accounts) connected to the user's Metricool account, including social network connections, timezones, and competitor settings.

- **Safety:** read-only, idempotent

### get_scheduled_posts

Retrieves scheduled (not yet published) posts for a brand within a date range.

- **Safety:** read-only, idempotent
- **Parameters:** `brand_id`, `from_date`, `to_date`, `timezone`, `extended_range`

### get_best_time_to_post_by_network

Returns a scored list of days and hours indicating the best times to post for a given social network, based on historical engagement data.

- **Safety:** read-only, idempotent
- **Parameters:** `brand_id`, `from_date`, `to_date`, `timezone`, `social_network`

### get_analytics_available_metrics

Lists all available Data Studio metrics, optionally filtered by network and connector type. Useful for discovering which metric IDs to pass to `get_analytics_data_by_metrics`.

- **Safety:** read-only, idempotent, local data (no API call)
- **Parameters:** `network` (optional), `connector` (optional)

### get_analytics_data_by_metrics

Pulls analytical data for specified metrics and date range. Returns rows of data matching the requested Data Studio field IDs.

- **Safety:** read-only, idempotent
- **Parameters:** `brand_id`, `from_date`, `to_date`, `metrics`

### create_scheduled_post

Schedules a new post to one or more social networks at a specific date and time. Supports network-specific options (Instagram Reels, YouTube Shorts, TikTok, LinkedIn polls, etc.).

- **Safety:** write operation, not idempotent (calling twice creates two posts)
- **Parameters:** `date`, `blog_id`, `info` (JSON with post data)

### update_scheduled_post

Updates an existing scheduled post. Requires the full post content with the desired modifications.

- **Safety:** write operation, idempotent
- **Parameters:** `id`, `uuid`, `blog_id`, `info` (JSON with full post data)

---

## Usage examples

### Example 1: Check your connected brands

**Prompt:**
> What social media accounts do I have connected in Metricool?

**What happens:** Claude calls `get_brand_settings` and returns a summary of your brands, connected networks, and timezones.

**Expected output:**
> You have 1 brand connected — "My Company". It has the following social accounts linked:
> - Instagram: @mycompany
> - X (Twitter): @mycompany
> - LinkedIn: My Company Page
> - TikTok: @mycompany
>
> Your timezone is set to Europe/Madrid.

---

### Example 2: Schedule a post at the best time

**Prompt:**
> Schedule a post on Instagram and X for tomorrow saying "Excited to announce our new feature! Check it out at mycompany.com 🚀". Pick the best time.

**What happens:** Claude calls `get_brand_settings` to get your brand ID and timezone, then `get_best_time_to_post_by_network` for both Instagram and X to determine the optimal posting time, and finally `create_scheduled_post` with the text and selected networks.

**Expected output:**
> I've scheduled your post for tomorrow at 10:30 AM (Europe/Madrid), which is the highest-scoring time slot for both Instagram and X this week.
>
> **Scheduled post:**
> - Text: "Excited to announce our new feature! Check it out at mycompany.com 🚀"
> - Networks: Instagram (Post), X
> - Date: 2026-03-28T10:30:00+01:00

---

### Example 3: Review your Instagram analytics

**Prompt:**
> Show me my Instagram engagement metrics for the last 30 days.

**What happens:** Claude calls `get_analytics_available_metrics` filtered by Instagram to discover the relevant metric IDs, then `get_analytics_data_by_metrics` with those IDs and the date range.

**Expected output:**
> Here are your Instagram metrics for Feb 25 – Mar 27, 2026:
>
> | Metric | Value |
> |---|---|
> | Followers | 12,450 (+320) |
> | Posts published | 18 |
> | Total reach | 45,200 |
> | Engagement rate | 4.2% |
> | Likes | 3,150 |
> | Comments | 287 |
>
> Your engagement rate is above the industry average of 3.5%.

---

### Example 4: Reschedule an existing post

**Prompt:**
> I have a post scheduled for Friday. Move it to Saturday at 5 PM.

**What happens:** Claude calls `get_scheduled_posts` to find the Friday post, confirms the change with the user, then calls `update_scheduled_post` with the updated date while preserving all other content.

**Expected output:**
> I found your scheduled post for Friday at 9:00 AM:
> - Text: "Weekend vibes! 🌴"
> - Networks: Instagram, Facebook
>
> Do you want me to move it to Saturday March 29 at 5:00 PM?
>
> *(after confirmation)*
>
> Done! Your post has been rescheduled to Saturday March 29 at 5:00 PM.

---

## Privacy policy

Metricool's privacy policy is available at:
**[https://metricool.com/privacy-policy/](https://metricool.com/privacy-policy/)**

This MCP server:
- Only accesses Metricool data on behalf of authenticated users.
- Does not store conversation content or prompts.
- Does not collect data beyond what is necessary to fulfill tool requests.
- Forwards authenticated API calls to the Metricool platform; no user data is persisted on the MCP server itself.

Terms of service: [https://metricool.com/legal-terms/](https://metricool.com/legal-terms/)

---

## Support

For issues, questions, or feedback related to the Metricool MCP connector:

- **Email:** [support@metricool.com](mailto:support@metricool.com)
- **Help center:** [https://help.metricool.com](https://help.metricool.com)
- **GitHub issues:** [https://github.com/vicampuzano/metricool-mcp/issues](https://github.com/vicampuzano/metricool-mcp/issues)
