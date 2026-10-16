#!/usr/bin/env python3
"""Servidor local do O Diario Virtual.

Serve a pagina e faz as buscas no lado do servidor (sem CORS, sem proxies
publicos). Tambem extrai imagem e texto das materias para o modo de leitura.

Uso:  python servidor.py      (depois abra http://localhost:8000)
Opcional: defina CURRENTS_API_KEY em vez de colar a chave na pagina.
"""
import base64
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "jornal-virtual.html")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE_TTL = 6 * 3600
_cache = {}


def fetch(url, headers=None, data=None, timeout=20, limit=None):
    h = {"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit) if limit else r.read(), \
                r.headers.get("Content-Type", "text/plain"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "text/plain"), url


def decode_bytes(body, ctype=""):
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I) or \
        re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
    enc = (m.group(1).decode() if m and isinstance(m.group(1), bytes) else
           (m.group(1) if m else "utf-8"))
    try:
        return body.decode(enc, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def public_http(url):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except OSError:
        return False
    for i in infos:
        ip = ipaddress.ip_address(i[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


# ---------- Links do Google Noticias -> link real ----------
def decode_b64(gid):
    try:
        b = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
    except Exception:
        return None
    if b.startswith(b"\x08\x13\x22"):
        b = b[3:]
    if b.endswith(b"\xd2\x01\x00"):
        b = b[:-3]
    if not b:
        return None
    n, off = b[0], 1
    if n & 0x80 and len(b) > 1:
        n, off = (n & 0x7F) | (b[1] << 7), 2
    s = b[off:off + n].decode("utf-8", "ignore")
    return s if s.startswith("http") else None


def resolve_google(url):
    m = re.search(r"articles/([^/?]+)", urllib.parse.urlparse(url).path)
    if not m:
        raise ValueError("link do Google Noticias invalido")
    gid = m.group(1)
    direct = decode_b64(gid)
    if direct:
        return direct
    code, body, ctype, _ = fetch(f"https://news.google.com/rss/articles/{gid}",
                                 headers={"Cookie": "CONSENT=YES+"})
    html = decode_bytes(body, ctype)
    sg = re.search(r'data-n-a-sg="([^"]+)"', html)
    ts = re.search(r'data-n-a-ts="([^"]+)"', html)
    if not sg or not ts:
        raise ValueError("nao foi possivel resolver o link do Google Noticias")
    inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,'
             'null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
             f'"{gid}",{ts.group(1)},"{sg.group(1)}"]')
    payload = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner]]]))
    code, body, ctype, _ = fetch(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        data=payload.encode())
    try:
        parsed = json.loads(body.decode("utf-8", "replace").split("\n\n")[1])[:-2]
        return json.loads(parsed[0][2])[1]
    except Exception:
        raise ValueError("nao foi possivel resolver o link do Google Noticias")


# ---------- Extracao de imagem e texto ----------
class Extractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "footer", "aside", "form",
            "header", "svg", "iframe", "button", "figcaption"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.title = {}, ""
        self.skip = self.art = 0
        self.in_p = self.in_title = False
        self.buf, self.p_all, self.p_art = [], [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = (a.get("property") or a.get("name") or "").lower()
            v = a.get("content")
            if k and v and k not in self.meta:
                self.meta[k] = v
        elif tag == "title":
            self.in_title = True
        elif tag in self.SKIP:
            self.skip += 1
        elif tag == "article":
            self.art += 1
        elif tag == "p" and not self.skip:
            self.in_p, self.buf = True, []
        elif tag == "br" and self.in_p:
            self.buf.append(" ")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "article" and self.art:
            self.art -= 1
        elif tag == "p" and self.in_p:
            self.in_p = False
            t = " ".join("".join(self.buf).split())
            if t:
                self.p_all.append(t)
                if self.art:
                    self.p_art.append(t)

    def handle_data(self, d):
        if self.in_title:
            self.title += d
        if self.in_p and not self.skip:
            self.buf.append(d)


def extract(html, base_url):
    ex = Extractor()
    try:
        ex.feed(html)
    except Exception:
        pass
    m = ex.meta
    good = lambda ps: [p for p in ps if len(p) >= 40]
    paras = good(ex.p_art) if len(good(ex.p_art)) >= 2 else good(ex.p_all)
    out, seen, total = [], set(), 0
    for p in paras:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        total += len(p)
        if len(out) >= 60 or total > 20000:
            break
    img = m.get("og:image") or m.get("twitter:image") or m.get("twitter:image:src") or ""
    if img:
        img = urllib.parse.urljoin(base_url, img)
        if not img.startswith("http"):
            img = ""
    return {
        "url": base_url,
        "title": (m.get("og:title") or ex.title).strip(),
        "site": m.get("og:site_name") or urllib.parse.urlparse(base_url).hostname or "",
        "description": (m.get("og:description") or m.get("description") or "").strip(),
        "image": img,
        "paragraphs": out,
    }


def get_article(url):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    real = url
    if urllib.parse.urlparse(url).hostname == "news.google.com":
        real = resolve_google(url)
    if not public_http(real):
        raise ValueError("endereco nao permitido")
    code, body, ctype, final = fetch(real, headers={"Accept": "text/html"}, limit=2_500_000)
    if code >= 400:
        raise ValueError(f"site respondeu HTTP {code}")
    data = extract(decode_bytes(body, ctype), final)
    _cache[url] = (time.time(), data)
    return data


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(PAGE, "rb") as f:
                    self.send(200, f.read(), "text/html; charset=utf-8")
            elif u.path == "/api/ping":
                self.send_json(200, {"ok": True})
            elif u.path == "/api/rss":
                target = (q.get("url") or [""])[0]
                if urllib.parse.urlparse(target).hostname != "news.google.com":
                    return self.send_json(400, {"error": "host nao permitido"})
                code, body, ctype, _ = fetch(target)
                self.send(code, body, ctype)
            elif u.path == "/api/currents":
                key = self.headers.get("X-Api-Key") or os.environ.get("CURRENTS_API_KEY", "")
                if not key:
                    return self.send_json(401, {"status": "error", "msg": "sem chave"})
                params = {"keywords": (q.get("keywords") or [""])[0],
                          "language": "pt", "apiKey": key}
                url = "https://api.currentsapi.services/v1/search?" + urllib.parse.urlencode(params)
                code, body, ctype, _ = fetch(url)
                self.send(code, body, ctype)
            elif u.path == "/api/article":
                target = (q.get("url") or [""])[0]
                if not target.startswith("http"):
                    return self.send_json(400, {"error": "url invalida"})
                self.send_json(200, get_article(target))
            else:
                self.send(404, b"nao encontrado", "text/plain")
        except ValueError as e:
            self.send_json(502, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.send_json(502, {"error": str(e), "status": "error", "msg": str(e)})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    # Na nuvem a plataforma define PORT e precisa que o servidor escute em 0.0.0.0.
    # No PC (sem PORT definida) continua so em 127.0.0.1.
    HOST = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"O Diario Virtual em http://localhost:{PORT}  (Ctrl+C para encerrar)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
