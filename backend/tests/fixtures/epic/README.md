Recorded from the live Epic endpoints on 2026-09-28 against a real library (#64).

- `library_page_*.json`: two pages of `library-service .../library/api/public/items?includeMetadata=true`,
  records as returned, a handful chosen to cover a game, the same catalogue item listed twice,
  an add-on, the engine, an Unreal Marketplace asset and a claimed weekly giveaway. The cursor
  is replaced; the last page of the real answer has no `nextCursor` key at all, as here.
- `catalog_by_namespace.json`: `catalog .../namespace/{ns}/bulk/items` answers, one per namespace,
  trimmed to the fields the provider reads.
- `oauth_token.json`: the shape of a token response, with invented tokens and account.
- `oauth_code_not_found.json`: Epic's answer to a spent authorization code, as recorded.
