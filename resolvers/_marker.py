def marker_token(embed_url: str) -> str:
    """The token after a ``crimson-<name>:`` prefix, or the bare embed when it has none."""
    token = embed_url.split(":", 1)[1] if ":" in embed_url else embed_url
    return token.strip().strip("/")
