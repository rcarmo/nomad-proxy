# Nomad Proxy

This is a tiny Python asyncio-based single-target web proxy relying only on the standard library that I hacked together so that I could use [my Supernote Nomad](https://taoofmac.com/space/reviews/2025/06/14/1530) as a drawing tablet via a tunnel from inside a VDI session (since of course the VDI environment isn't connected to my local network).

It serves a form (POST) to select a target URL, sets a cookie and 303-redirects back to `/`, then transparently proxies subsequent GET requests to that target until the remote server fails—at which point it clears the active target cookie and shows the form again.

You can go to `/reset` to clear the active target manually (the last-used target is retained for pre-fill).

> Disclaimer: this was quickly hacked together for a specific use case and is not production-ready software, plus I did resort to GitHub Copilot/GPT-5 to a) figure out how to ensure the MJPEG stream from the Nomad was passed through successfully b) tidy up docstrings and c) get a quick draft of this `README`. Use at your own risk.

## Features

- Pure standard library (asyncio + http.client)
- URL selection form at root (`/`) submitted via POST (privacy: target URL not kept in browser history/query)
- Cookies: `ProxyTarget` (active) and `LastTarget` (pre-fill convenience)
- 303 redirect after selection to provide a clean root URL
- Transparent forwarding of subsequent GET paths to the selected base
- MJPEG streaming passthrough for `.mjpeg` URLs (multipart/x-mixed-replace) without buffering
- `/reset` endpoint to drop only the active target while keeping `LastTarget`
- Environment configuration via `PROXY_HOST` and `PROXY_PORT`

## Limitations

- GET only for proxied traffic (no POST/PUT/DELETE forwarding, no CONNECT)
- Minimal header filtering (drops `Transfer-Encoding`, forces `Connection: close`)
- No HTML/JS/CSS rewriting (absolute links may escape proxy)
- Single active target per client (cookie-based) — no multi-session multiplexing
- Basic error handling; remote failures return 502 and clear active target

## Running

Requires Python 3.10+ (for best asyncio behavior). I am currently running this behind an OIDC-authenticated Cloudflare tunnel for TLS and access control, so there's no built-in security.

I have also provided a sample `nomad-proxy.service` systemd unit file for running this as a user service on Linux systems with systemd support.

So if it breaks, you get to keep all of the pieces...
