from fastapi.responses import Response, StreamingResponse


def proxy_response(status, content_type, headers, payload, *, forward_bytes_headers=False):
    """Turn a ``proxy_fetch`` result into a response: buffered bytes for a
    rewritten HLS playlist, or a streamed body for a segment. Only Jellyfin needs
    upstream headers forwarded on a buffered playlist."""
    if isinstance(payload, (bytes, bytearray)):
        return Response(
            content=payload,
            status_code=status,
            media_type=content_type,
            headers=headers if forward_bytes_headers else None,
        )
    return StreamingResponse(payload, status_code=status, media_type=content_type, headers=headers)
