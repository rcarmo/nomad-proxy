#!/usr/bin/env python3
"""
Minimal asyncio-based single-target web proxy with form selection.
Only uses Python standard library and targets Nomad screen sharing use case.
"""
import asyncio
import ssl
import http.client
import urllib.parse
from os import getenv
from typing import Dict, Tuple, Optional

USER_AGENT = "NomadAsyncProxy/0.2"
DEFAULT_HOST_ENV = "PROXY_HOST"
DEFAULT_PORT_ENV = "PROXY_PORT"

def html_escape(s: str) -> str:
    """Minimal HTML escaping for attribute injection prevention."""
    return (s.replace('&', '&amp;')
             .replace('<', '&lt;')
             .replace('>', '&gt;')
             .replace('"', '&quot;')
             .replace("'", '&#x27;'))

def build_form_html(prefill: Optional[str]) -> bytes:
    value_attr = f" value=\"{html_escape(prefill)}\"" if prefill else ""
    return f"""<!doctype html>
<html lang='en'>
    <head>
        <meta charset='utf-8'>
        <title>Nomad Proxy</title>
        <meta name='viewport' content='width=device-width,initial-scale=1'>
        <style>
            :root {{ color-scheme: light dark; }}
            body {{ font-family: system-ui,-apple-system,Segoe UI,Roboto,sans-serif; margin: 3em auto; max-width: 42em; line-height: 1.4; }}
            h1 {{ margin-top: 0; font-size: 1.6rem; }}
            form {{ margin-top: 1.5em; }}
            label {{ font-weight: 600; display: block; margin-bottom: .5em; }}
            input[type=url] {{ width: 100%; padding: .6em .7em; font-size: 1rem; border: 1px solid #888; border-radius: .4em; }}
            input[type=url]:focus {{ outline: 2px solid #4a90e2; }}
            button {{ padding: .65em 1.4em; font-size: 1rem; font-weight: 600; border: none; border-radius: .4em; background: #4a90e2; color: #fff; cursor: pointer; }}
            button:hover {{ background: #357ABD; }}
            footer {{ margin-top: 2.5em; font-size: .8em; color: #666; }}
        </style>
    </head>
    <body>
        <h1>Nomad Proxy</h1>
        <form method='post' action='/' aria-label='Nomad target selection'>
            <label for='target'>Enter your Nomad's screen sharing URL</label>
            <input id='target' name='target' type='url' placeholder='http://192.168.1.203:8080' required pattern='https?://.+' autofocus{value_attr}>
            <p><button type='submit'>Connect</button></p>
        </form>
        <footer>When the server disconnects, reload this page to choose a new target.</footer>
    </body>
</html>""".encode()

MAX_HEADER_SIZE = 32 * 1024  # bytes
MAX_BODY_SIZE = 16 * 1024 * 1024  # safety cap (16MB)

COOKIE_NAME = "ProxyTarget"
LAST_COOKIE_NAME = "LastTarget"

async def read_request(reader: asyncio.StreamReader) -> Tuple[str, str, str, Dict[str, str]]:
    """Read and parse a single HTTP request without body (body handled separately).
    Returns (method, path, version, headers). Raises ValueError/ConnectionError on parse issues."""
    request_line = await reader.readline()
    if not request_line:
        raise ConnectionError("Empty request line")
    parts = request_line.decode(errors='ignore').strip().split()
    if len(parts) != 3:
        raise ValueError("Malformed request line")
    method, path, version = parts
    # Read headers
    headers: Dict[str, str] = {}
    total = 0
    while True:
        line = await reader.readline()
        if not line:
            break
        if line in (b"\r\n", b"\n"):
            break
        total += len(line)
        if total > MAX_HEADER_SIZE:
            raise ValueError("Headers too large")
        try:
            k, v = line.decode(errors='ignore').split(':', 1)
        except ValueError:
            continue
        headers[k.strip()] = v.strip()
    return method.upper(), path, version, headers

def parse_cookies(header_value: str) -> Dict[str, str]:
    cookies = {}
    if not header_value:
        return cookies
    for part in header_value.split(';'):
        if '=' in part:
            k, v = part.strip().split('=', 1)
            cookies[k] = v
    return cookies

def build_response_status(writer: asyncio.StreamWriter, status_code: int, reason: str = 'OK'):
    writer.write(f"HTTP/1.1 {status_code} {reason}\r\n".encode())

def send_basic_headers(writer: asyncio.StreamWriter, extra: Optional[Dict[str, str]] = None):
    headers = {
        'Server': USER_AGENT,
        'Connection': 'close'
    }
    if extra:
        headers.update(extra)
    for k, v in headers.items():
        if isinstance(v, (list, tuple)):
            for item in v:
                writer.write(f"{k}: {item}\r\n".encode())
        else:
            writer.write(f"{k}: {v}\r\n".encode())

def validate_target(url: str) -> Optional[urllib.parse.ParseResult]:
    """Validate incoming target URL is http/https and has a network location."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return parsed

def fetch_via_httpclient(parsed: urllib.parse.ParseResult, path_override: Optional[str] = None) -> Tuple[int, str, Dict[str, str], bytes]:
    """Blocking single GET request (delegated via executor when called async)."""
    conn_cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    remote_host = parsed.netloc
    target_path = path_override if path_override is not None else (parsed.path or '/')
    if parsed.query:
        target_path += ('?' + parsed.query)
    conn = conn_cls(remote_host, timeout=15)
    conn.request('GET', target_path, headers={'Host': remote_host, 'User-Agent': USER_AGENT, 'Accept': '*/*'})
    resp = conn.getresponse()
    body = resp.read(MAX_BODY_SIZE)
    status = resp.status
    reason = resp.reason
    hdrs = {k: v for k, v in resp.getheaders()}
    conn.close()
    return status, reason, hdrs, body

async def fetch_remote(parsed: urllib.parse.ParseResult, path: Optional[str] = None) -> Tuple[int, str, Dict[str, str], bytes]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fetch_via_httpclient, parsed, path)

def combine_paths(base: urllib.parse.ParseResult, requested_path: str) -> str:
    # Ensure remote path joining without losing base.path prefix
    base_path = base.path or '/'
    if not base_path.endswith('/') and requested_path != '/':
        # treat base_path as a prefix directory only if user requested extra path
        combined = base_path + requested_path
    else:
        if base_path == '/':
            combined = requested_path
        else:
            combined = urllib.parse.urljoin(base_path + ('/' if not base_path.endswith('/') else ''), requested_path.lstrip('/'))
    if not combined.startswith('/'):
        combined = '/' + combined
    return combined

async def stream_mjpeg(parsed: urllib.parse.ParseResult, remote_path: str, client_writer: asyncio.StreamWriter, cookie_to_set: Optional[str]):
    """Stream an MJPEG (multipart/x-mixed-replace) resource without buffering full content.
    Maintains transparency; stops quietly on client disconnect."""
    remote_host = parsed.hostname
    if remote_host is None:
        raise ValueError("No hostname in target URL")
    remote_port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    ssl_ctx = None
    if parsed.scheme == 'https':
        ssl_ctx = ssl.create_default_context()
    # Establish remote connection
    reader, remote_writer = await asyncio.open_connection(remote_host, remote_port, ssl=ssl_ctx)
    # Build path with query if any embedded in parsed
    full_path = remote_path
    request = (
        f"GET {full_path} HTTP/1.1\r\n"
        f"Host: {parsed.netloc}\r\n"
        f"User-Agent: {USER_AGENT}\r\n"
        f"Accept: */*\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    remote_writer.write(request)
    await remote_writer.drain()
    # Parse status line
    status_line = await reader.readline()
    if not status_line:
        raise ConnectionError("Empty status line from remote stream")
    parts = status_line.decode(errors='ignore').strip().split()
    if len(parts) < 3:
        raise ValueError("Malformed remote status line")
    try:
        status_code = int(parts[1])
    except ValueError as exc:
        raise ValueError("Invalid status code from remote stream") from exc
    reason = ' '.join(parts[2:])
    build_response_status(client_writer, status_code, reason)
    # Read headers
    remote_headers: Dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        try:
            k, v = line.decode(errors='ignore').split(':', 1)
        except ValueError:
            continue
        remote_headers[k.strip()] = v.strip()
    # Adjust/forward headers: keep Transfer-Encoding & Content-Type intact, omit Content-Length
    # Remove Content-Length if present (stream is indefinite)
    remote_headers.pop('Content-Length', None)
    # We'll send our server header but retain others; ensure connection close
    remote_headers['Connection'] = 'close'
    if cookie_to_set:
        remote_headers['Set-Cookie'] = cookie_to_set
    send_basic_headers(client_writer, remote_headers)
    client_writer.write(b"\r\n")
    # Stream body chunks with graceful client disconnect handling
    try:
        while True:
            try:
                chunk = await reader.read(8192)
            except asyncio.CancelledError:
                # Server shutting down; abort stream
                break
            if not chunk:
                break
            try:
                client_writer.write(chunk)
                # If client already closed, stop streaming
                if client_writer.is_closing():
                    break
                await client_writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                # Browser went away mid-stream; exit quietly
                break
    finally:
        try:
            remote_writer.close()
        except (OSError, ssl.SSLError):
            pass
        # remote_writer.wait_closed() can hang on some abrupt TLS terminations; omit for responsiveness

async def handle_proxy(method: str, path: str, headers: Dict[str, str], writer: asyncio.StreamWriter, body: Optional[bytes]):
    cookies = parse_cookies(headers.get('Cookie', ''))
    query = ''
    url_path = path
    if '?' in path:
        url_path, query = path.split('?', 1)
    params = urllib.parse.parse_qs(query)

    # Special reset handler: /reset clears active target cookie (keeps last) and shows form
    if url_path == '/reset':
        last_cookie = cookies.get(LAST_COOKIE_NAME)
        prefill = None
        if last_cookie:
            try:
                prefill = urllib.parse.unquote(last_cookie)
            except ValueError:
                prefill = None
        form = build_form_html(prefill)
        build_response_status(writer, 200, 'OK')
        send_basic_headers(writer, {
            'Content-Type': 'text/html; charset=utf-8',
            'Set-Cookie': f'{COOKIE_NAME}=deleted; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT',
            'Content-Length': str(len(form))
        })
        writer.write(b"\r\n")
        writer.write(form)
        return

    # Gather potential new target (query + POST body if present)
    new_target = params.get('target', [None])[0]
    if body and (headers.get('Content-Type','').startswith('application/x-www-form-urlencoded')):
        try:
            post_params = urllib.parse.parse_qs(body.decode('utf-8'), keep_blank_values=True)
            if not new_target and 'target' in post_params:
                new_target = post_params['target'][0]
        except UnicodeDecodeError:
            pass
    target_cookie = cookies.get(COOKIE_NAME)
    last_cookie = cookies.get(LAST_COOKIE_NAME)

    selected: Optional[urllib.parse.ParseResult] = None
    initial_fetch_override = None
    cookie_to_set: Optional[str] = None

    if new_target:
        parsed = validate_target(new_target)
        if not parsed:
            build_response_status(writer, 400, 'Bad Request')
            send_basic_headers(writer, {'Content-Type': 'text/plain'})
            writer.write(b"\r\nInvalid target URL")
            return
        selected = parsed
        target_cookie_value = urllib.parse.quote(new_target, safe='')
        cookie_to_set = f'{COOKIE_NAME}={target_cookie_value}; Path=/; HttpOnly'
        last_cookie_to_set = f'{LAST_COOKIE_NAME}={target_cookie_value}; Path=/; HttpOnly'
        # After POST selection we issue a redirect rather than immediate fetch to clean URL
        if method == 'POST':
            build_response_status(writer, 303, 'See Other')
            send_basic_headers(writer, {
                'Location': '/',
                'Set-Cookie': [cookie_to_set, last_cookie_to_set],
                'Content-Length': '0'
            })
            writer.write(b"\r\n")
            return
        else:
            # GET fallback: fetch immediately (legacy behavior)
            initial_fetch_override = parsed.path or '/'
    else:
        cookie_to_set = None  # only set cookies on new selection
    if (not selected) and target_cookie:
        try:
            decoded = urllib.parse.unquote(target_cookie)
            parsed = validate_target(decoded)
            if parsed:
                selected = parsed
        except ValueError:
            selected = None

    if not selected:
        # Serve selection form with prefill from LastTarget if present
        prefill = None
        if last_cookie and not target_cookie:
            try:
                prefill = urllib.parse.unquote(last_cookie)
            except ValueError:
                prefill = None
        form = build_form_html(prefill)
        build_response_status(writer, 200, 'OK')
        send_basic_headers(writer, {'Content-Type': 'text/html; charset=utf-8', 'Content-Length': str(len(form))})
        writer.write(b"\r\n")
        writer.write(form)
        return

    # Proxy request
    remote_path = initial_fetch_override if initial_fetch_override is not None else combine_paths(selected, url_path)

    # If this looks like an MJPEG stream, do a direct streaming proxy (no buffering, no Content-Length)
    if remote_path.lower().endswith('.mjpeg'):
        try:
            await stream_mjpeg(selected, remote_path, writer, cookie_to_set)
        except (OSError, TimeoutError, http.client.HTTPException, ValueError, ConnectionError) as e:
            build_response_status(writer, 502, 'Bad Gateway')
            send_basic_headers(writer, {
                'Content-Type': 'text/plain',
                'Set-Cookie': f'{COOKIE_NAME}=deleted; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT'
            })
            msg = f"MJPEG stream failed: {e}".encode()
            writer.write(f"Content-Length: {len(msg)}\r\n\r\n".encode())
            writer.write(msg)
        return
    if params and not initial_fetch_override and 'target' in params:
        # if leftover target param (edge), remove it from forwarded path query
        pass
    # Append original query (excluding target)
    if query:
        filtered = urllib.parse.parse_qs(query)
        filtered.pop('target', None)
        if filtered:
            remote_query = urllib.parse.urlencode([(k, v2) for k, vs in filtered.items() for v2 in vs])
            if remote_query:
                if '?' in remote_path:
                    remote_path += '&' + remote_query
                else:
                    remote_path += '?' + remote_query
    try:
        status, reason, resp_headers, body = await fetch_remote(selected, remote_path)
    except (OSError, TimeoutError, http.client.HTTPException) as e:
        # Remote failed -> clear cookie & show form again
        build_response_status(writer, 502, 'Bad Gateway')
        send_basic_headers(writer, {
            'Content-Type': 'text/plain',
            'Set-Cookie': f'{COOKIE_NAME}=deleted; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT'
        })
        msg = f"Remote fetch failed: {e}".encode()
        writer.write(f"Content-Length: {len(msg)}\r\n\r\n".encode())
        writer.write(msg)
        return

    # Build response
    build_response_status(writer, status, reason)
    # Filter/adjust headers
    excluded = {'connection', 'transfer-encoding'}
    response_header_block = {}
    for k, v in resp_headers.items():
        if k.lower() in excluded:
            continue
        response_header_block[k] = v
    response_header_block['Content-Length'] = str(len(body))
    if cookie_to_set:
        # Include both cookies freshly for clarity
        set_cookies = [cookie_to_set]
        # Preserve existing last target even if remote changed path
        if last_cookie:
            set_cookies.append(f'{LAST_COOKIE_NAME}={last_cookie}; Path=/; HttpOnly')
        else:
            set_cookies.append(f'{LAST_COOKIE_NAME}={urllib.parse.quote(selected.geturl(), safe="")}; Path=/; HttpOnly')
        response_header_block['Set-Cookie'] = set_cookies
    # Send headers
    send_basic_headers(writer, response_header_block)
    writer.write(b"\r\n")
    writer.write(body)

async def client_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    body: Optional[bytes] = None
    try:
        method, path, _version, headers = await read_request(reader)
    except (ValueError, ConnectionError, asyncio.IncompleteReadError):
        build_response_status(writer, 400, 'Bad Request')
        send_basic_headers(writer, {'Content-Type': 'text/plain'})
        writer.write(b"\r\nBad Request")
        await writer.drain()
        writer.close()
        return

    if method == 'POST':
        # Read small form body
        clen = int(headers.get('Content-Length','0') or '0')
        if clen > 4096:
            build_response_status(writer, 413, 'Payload Too Large')
            send_basic_headers(writer, {'Content-Type': 'text/plain'})
            writer.write(b"\r\nForm too large")
            await writer.drain()
            writer.close()
            return
        if clen:
            body = await reader.readexactly(clen)
    elif method not in ('GET', 'HEAD'):
        build_response_status(writer, 405, 'Method Not Allowed')
        send_basic_headers(writer, {'Allow': 'GET, HEAD, POST', 'Content-Type': 'text/plain'})
        writer.write(b"\r\nMethod not supported")
        await writer.drain()
        writer.close()
        return

    try:
        await handle_proxy(method, path, headers, writer, body)
        # For streaming responses writer may already be mid-transfer; is_closing check prevents double close.
        if not writer.is_closing():
            try:
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                # Client vanished before final drain; ignore.
                pass
            writer.close()
    except asyncio.CancelledError:
        # Allow cancellation to bubble up (server shutdown)
        writer.close()
        raise

async def main(host: str = '127.0.0.1', port: int = 8080):
    server = await asyncio.start_server(client_handler, host, port)
    addrs = ', '.join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"Proxy listening on {addrs}")
    async with server:
        await server.serve_forever()

if __name__ == '__main__':
    run_host = getenv(DEFAULT_HOST_ENV, '127.0.0.1')
    run_port = int(getenv(DEFAULT_PORT_ENV, '8080'))
    try:
        asyncio.run(main(host=run_host, port=run_port))
    except KeyboardInterrupt:
        print("\nShutting down.")
