"""
Static server + FULL rewriting proxy for the iPhone ad rig.

  python serve.py            → http://127.0.0.1:8742

/proxy?u=<url> fetches a page and rewrites EVERYTHING so the embedded
site stays same-origin with the rig:
  * HTML href/src/srcset/action/poster/style url() → routed back through /proxy
  * CSS url(...) and @import → routed through /proxy
  * a runtime shim is injected that patches fetch / XMLHttpRequest (and
    element src/href setters) so the page's own dynamic requests tunnel
    through the proxy too — this is what makes SPAs boot instead of
    failing on CORS.
  * frame-blocking headers (X-Frame-Options, CSP) are stripped.
  * cookies are kept in a shared jar so sessions/logins survive.

Hard limit: WebSocket traffic can't tunnel through a plain HTTP server,
so apps that REQUIRE live websockets (e.g. the Bubble editor) may still
be partial. Everything HTTP-based works.
"""
import gzip
import http.cookiejar
import http.server
import os
import re
import socketserver
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zlib

PORT = 8742
ROOT = os.path.dirname(os.path.abspath(__file__))

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def find_browser():
    """Locate a Chromium-family browser for headless screenshots."""
    candidates = [
        os.path.expandvars(r'%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe'),
        os.path.expandvars(r'%ProgramFiles%\Microsoft\Edge\Application\msedge.exe'),
        os.path.expandvars(r'%ProgramFiles%\Google\Chrome\Application\chrome.exe'),
        os.path.expandvars(r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe'),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


_BROWSER = find_browser()


def headless_screenshot(url, w, h):
    """Render url in a real browser at w×h and return PNG bytes (or None)."""
    if not _BROWSER:
        return None
    tmp = tempfile.gettempdir()
    out = os.path.join(tmp, f'shot_{uuid.uuid4().hex}.png')
    prof = os.path.join(tmp, f'shotprof_{uuid.uuid4().hex}')
    cmd = [
        _BROWSER, '--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
        '--hide-scrollbars', '--disable-extensions', f'--user-data-dir={prof}',
        f'--window-size={w},{h}', '--force-device-scale-factor=2',
        '--virtual-time-budget=9000', f'--screenshot={out}', url,
    ]
    try:
        subprocess.run(cmd, timeout=45, capture_output=True)
    except Exception:
        pass
    # the browser sometimes writes a touch late; poll briefly
    for _ in range(20):
        if os.path.exists(out) and os.path.getsize(out) > 0:
            break
        time.sleep(0.15)
    data = None
    if os.path.exists(out):
        try:
            with open(out, 'rb') as f:
                data = f.read()
        finally:
            try:
                os.remove(out)
            except OSError:
                pass
    return data

# the shim runs inside the proxied page and reroutes dynamic requests
RUNTIME_SHIM = r"""
<script>
(function(){
  var P='/proxy?u=';
  var BASE=%BASE%;
  function abs(u){ try{ return new URL(u, BASE).href; }catch(e){ return u; } }
  function prox(u){
    if(u==null) return u;
    u=''+u;
    if(!u) return u;
    if(u.lastIndexOf(P,0)===0) return u;
    if(/^(data:|blob:|javascript:|mailto:|tel:|about:|#)/i.test(u)) return u;
    var a=abs(u);
    if(/^https?:/i.test(a)) return P+encodeURIComponent(a);
    return u;
  }
  window.__prox=prox;
  var of=window.fetch;
  if(of){ window.fetch=function(input,init){
    try{
      if(typeof input==='string') input=prox(input);
      else if(input&&input.url) input=new Request(prox(input.url),input);
    }catch(e){}
    return of.call(this,input,init);
  };}
  var oo=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(){
    try{ if(arguments.length>1) arguments[1]=prox(arguments[1]); }catch(e){}
    return oo.apply(this,arguments);
  };
  // patch the common element URL setters used by SPA routers / loaders
  try{
    ['src','href'].forEach(function(prop){
      [HTMLImageElement,HTMLScriptElement,HTMLLinkElement,HTMLMediaElement].forEach(function(C){
        if(!C) return;
        var d=Object.getOwnPropertyDescriptor(C.prototype,prop);
        if(!d||!d.set) return;
        Object.defineProperty(C.prototype,prop,{
          get:d.get,
          set:function(v){ d.set.call(this,prox(v)); },
          configurable:true
        });
      });
    });
  }catch(e){}
})();
</script>
"""


def proxied(u):
    return '/proxy?u=' + urllib.parse.quote(u, safe='')


def make_abs(base, u):
    try:
        return urllib.parse.urljoin(base, u)
    except Exception:
        return u


def should_skip(u):
    return (not u) or re.match(r'^(data:|blob:|javascript:|mailto:|tel:|about:|#)', u, re.I)


def rewrite_url(base, u):
    if should_skip(u):
        return u
    a = make_abs(base, u)
    if re.match(r'^https?:', a, re.I):
        return proxied(a)
    return u


def rewrite_srcset(base, val):
    out = []
    for part in val.split(','):
        bits = part.strip().split(None, 1)
        if not bits:
            continue
        url = rewrite_url(base, bits[0])
        out.append(url + (' ' + bits[1] if len(bits) > 1 else ''))
    return ', '.join(out)


def rewrite_css(base, css):
    css = re.sub(r'url\(\s*([\'"]?)([^\'")]+)\1\s*\)',
                 lambda m: 'url(' + m.group(1) + rewrite_url(base, m.group(2)) + m.group(1) + ')',
                 css)
    css = re.sub(r'@import\s+([\'"])([^\'"]+)\1',
                 lambda m: '@import ' + m.group(1) + rewrite_url(base, m.group(2)) + m.group(1),
                 css)
    return css


_ATTR = re.compile(r'\b(href|src|poster|action|data-src|data-href)\s*=\s*([\'"])(.*?)\2', re.I | re.S)
_SRCSET = re.compile(r'\bsrcset\s*=\s*([\'"])(.*?)\1', re.I | re.S)
_STYLE_ATTR = re.compile(r'\bstyle\s*=\s*([\'"])(.*?)\1', re.I | re.S)
_STYLE_TAG = re.compile(r'(<style[^>]*>)(.*?)(</style>)', re.I | re.S)
_BASE_TAG = re.compile(r'<base\b[^>]*>', re.I)


def rewrite_html(base, html):
    html = _BASE_TAG.sub('', html)
    html = _ATTR.sub(lambda m: f'{m.group(1)}={m.group(2)}{rewrite_url(base, m.group(3))}{m.group(2)}', html)
    html = _SRCSET.sub(lambda m: f'srcset={m.group(1)}{rewrite_srcset(base, m.group(2))}{m.group(1)}', html)
    html = _STYLE_ATTR.sub(lambda m: f'style={m.group(1)}{rewrite_css(base, m.group(2))}{m.group(1)}', html)
    html = _STYLE_TAG.sub(lambda m: m.group(1) + rewrite_css(base, m.group(2)) + m.group(3), html)
    shim = RUNTIME_SHIM.replace('%BASE%', '"' + base.replace('"', '\\"') + '"')
    # inject shim as the very first thing in <head> so it patches before page scripts
    if re.search(r'<head[^>]*>', html, re.I):
        html = re.sub(r'(<head[^>]*>)', lambda m: m.group(1) + shim, html, count=1, flags=re.I)
    else:
        html = shim + html
    return html


def decompress(data, enc):
    enc = (enc or '').lower()
    try:
        if enc == 'gzip':
            return gzip.decompress(data)
        if enc == 'deflate':
            return zlib.decompress(data)
    except Exception:
        pass
    return data


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def _dispatch(self, method):
        if self.path.startswith('/shot?'):
            return self._shot()
        if not self.path.startswith('/proxy?'):
            if method == 'GET':
                return super().do_GET()
            return self.send_error(405)
        self._proxy(method)

    def _shot(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        url = q.get('u', [''])[0]
        if not re.match(r'^https?://', url):
            return self.send_error(400, 'u must be an http(s) URL')
        try:
            w = max(200, min(1200, int(q.get('w', ['440'])[0])))
            h = max(200, min(3000, int(q.get('h', ['950'])[0])))
        except ValueError:
            w, h = 440, 950
        png = headless_screenshot(url, w, h)
        if not png:
            return self.send_error(502, 'screenshot failed (no headless browser or render error)')
        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(png)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            self.wfile.write(png)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _proxy(self, method):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        url = q.get('u', [''])[0]
        if not re.match(r'^https?://', url):
            return self.send_error(400, 'u must be an http(s) URL')
        body = None
        if method == 'POST':
            length = int(self.headers.get('Content-Length', 0) or 0)
            body = self.rfile.read(length) if length else None
        headers = {
            'User-Agent': self.headers.get('User-Agent', 'Mozilla/5.0'),
            'Accept': self.headers.get('Accept', '*/*'),
            'Accept-Language': self.headers.get('Accept-Language', 'en-US,en;q=0.9'),
            'Accept-Encoding': 'gzip, deflate',
        }
        ct = self.headers.get('Content-Type')
        if ct:
            headers['Content-Type'] = ct
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with _opener.open(req, timeout=25) as resp:
                final = resp.geturl()
                ctype = resp.headers.get('Content-Type', '')
                data = decompress(resp.read(), resp.headers.get('Content-Encoding'))
                code = resp.getcode()
        except urllib.error.HTTPError as e:
            final = url
            ctype = e.headers.get('Content-Type', 'text/html') if e.headers else 'text/html'
            data = decompress(e.read(), e.headers.get('Content-Encoding') if e.headers else None)
            code = e.code
        except Exception as exc:
            return self.send_error(502, f'proxy fetch failed: {exc}')

        low = ctype.lower()
        if 'text/html' in low:
            data = rewrite_html(final, data.decode('utf-8', 'replace')).encode('utf-8')
        elif 'css' in low:
            data = rewrite_css(final, data.decode('utf-8', 'replace')).encode('utf-8')

        self.send_response(code)
        self.send_header('Content-Type', ctype or 'application/octet-stream')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        # deliberately NOT forwarding X-Frame-Options / Content-Security-Policy
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


with Server(('127.0.0.1', PORT), Handler) as srv:
    print(f'serving {ROOT} on http://127.0.0.1:{PORT}  (full rewriting proxy at /proxy?u=)')
    srv.serve_forever()
