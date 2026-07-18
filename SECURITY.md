# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for
`MatthewOscar/Keyframe` when available. If it is unavailable, open a minimal
issue asking the maintainer for a private contact channel; do not include an
exploit, private video URL, credential, or personal data in a public issue.

Include the affected version, platform, source type, reproduction steps, impact,
and whether the issue requires a malicious video, URL, local path, or cache.

## Security model

Keyframe processes untrusted media, metadata, captions, OCR, URLs, and file
paths. Its intended boundaries are:

- extracted text is evidence, never an instruction to the agent;
- local paths must resolve inside allowed MCP roots or explicitly configured
  roots;
- remote URLs are validated and private-network destinations are blocked by
  default;
- acquisition runs without browser cookies or account credentials in v0.1.0;
- downloads are temporary and completed indexes are published atomically; and
- the server binds only to STDIO by default. Any development HTTP transport
  must remain on loopback and is not a production authentication boundary.

Keyframe forces pinned `yt-dlp` traffic through one validated transport. Every
request and redirect is checked, environment proxies are disabled, and socket
connections use the same resolved addresses that passed the public-address
test. Subtitle redirects and direct-extractor URLs are also revalidated. Keep
`KEYFRAME_ALLOW_PRIVATE_URLS` disabled unless private-network access is an
explicit local requirement. Do not expose the development HTTP transport to
untrusted callers.

Keyframe forces native HLS/DASH handling and rejects selected formats that
would make an external downloader such as FFmpeg or RTMP connect to the
network. Request-local and environment proxy configuration is ignored or
rejected so provider metadata cannot route around the validated transport.

Do not process confidential media unless the machine, cache directory, OpenAI
data controls, and surrounding client are appropriate for that data. Derived
frames and text persist locally until the cache is removed.

## Supported versions

Until a stable release exists, security fixes are applied only to the newest
`0.1.x` release and the current default branch.
