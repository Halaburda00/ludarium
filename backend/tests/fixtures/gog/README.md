Answers recorded from GOG on 2026-10-07 (#128), trimmed to a handful of
products. The tokens, the session, the user id and the profile name are
replaced. Product ids, titles and slugs are GOG's public catalogue. The second
statistics page is made up, to have one; the first is as measured.

| File | Endpoint |
|---|---|
| `oauth_token.json` | `POST auth.gog.com/token` |
| `oauth_invalid_grant.json` | the same, for a spent code or an unknown token (400) |
| `products_page_*.json` | `embed.gog.com/account/getFilteredProducts?mediaType=1&page=N` |
| `owned.json` | `embed.gog.com/user/data/games`, with a pack and an id the catalogue does not know |
| `user.json` | `embed.gog.com/userData.json`, only the fields read |
| `stats_page_*.json` | `www.gog.com/u/{username}/games/stats?page=N`, with a game that has no statistics (`[]`) |
| `catalog_ids.json` | `api.gog.com/products?ids=…`, an add-on and a pack |
| `v2_1256837418.json` | `api.gog.com/v2/games/1256837418`, the add-on's `requiresGames` |
