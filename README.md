# Deku Deals MCP Server

MCP server for searching games, tracking prices, and managing wishlists on dekudeals.com. Focused on the Canadian Nintendo eShop.

## Tools

- **search_games** — Search for games by name, optionally filter by discount
- **get_game_details** — Get prices, metadata, and store listings for a specific game
- **get_wishlist** — View your current wishlist with prices
- **add_to_wishlist** — Add a game to your wishlist with price alert preferences
- **remove_from_wishlist** — Remove a game from your wishlist
- **add_to_collection** — Mark a game as owned
- **get_current_sales** — Browse games currently on sale

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set up authentication

This server reads session cookies from `~/.mcp-credentials/dekudeals.json`, written by the [MCP Auth Bridge](../mcp-auth-bridge/) Chrome extension.

1. Install MCP Auth Bridge in Chrome
2. Log into dekudeals.com
3. Click the extension icon and Save Deku Deals

### 3. Add to Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "dekudeals": {
      "command": "python3",
      "args": ["/Users/you/dekudeals-mcp-server/dekudeals_mcp_server.py"],
      "env": {
        "DEKUDEALS_CREDENTIALS": "~/.mcp-credentials/dekudeals.json"
      }
    }
  }
}
```

## Auth Notes

- Read operations (search, game details) work without auth
- Write operations (wishlist, collection) require a valid session cookie
- If the session expires, the server returns a helpful message telling you to re-authenticate via the Chrome extension
