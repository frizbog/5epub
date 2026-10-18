#!/usr/bin/env python3
"""Build an EPUB 3 from a JSON file. Standard library only.

Usage: json2epub.py book.json [-o book.epub]

JSON shape (see example.json):
{
  "title": "...", "author": "...", "language": "en", "identifier": "optional",
  "cover": "cover.jpg",
  "chapters":   [{"id": "intro", "title": "...", "content": "<p>HTML or plain text</p>"}],
  "appendices": [{"id": "a", "title": "...", "content": "..."}],
  "index": [{"term": "Whale", "targets": ["intro", "ch2#whale"]}]
}
Content is HTML (or plain text, split on blank lines). <img src> may be a path
relative to the JSON file, or an http(s) URL; images are embedded. Links of the
form href="#chapter-id" or "#chapter-id#anchor" point to other sections.
"""
import argparse
import base64
import hashlib
import html
import io
import json
import mimetypes
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlparse

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}
DEPENDENCY_ATTRS = {
    "audio": ("src",), "base": ("href",), "embed": ("src",), "iframe": ("src",),
    "image": ("href", "xlink:href"), "input": ("src",), "link": ("href",),
    "object": ("data",), "script": ("src",), "source": ("src", "srcset"),
    "track": ("src",), "use": ("href", "xlink:href"), "video": ("src", "poster"),
}
REMOTE_RESOURCE_RE = re.compile(
    r'''(?:\b(?:src|poster|data)\s*=\s*["']\s*https?://|url\(\s*["']?\s*https?://)''',
    re.IGNORECASE,
)
IMAGE_CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "images"
CSS = ("body{font-family:serif;line-height:1.5}img{max-width:100%;height:auto}"
       "h1{margin-top:1em}.index li{margin:.2em 0}.letters a{margin-right:.6em}"
       ".mini{font-size:.9em;border-left:3px solid #888;padding-left:.8em;list-style:none}.mini li.d1{margin-left:1.2em}"
       "p,li,dd{orphans:3;widows:3}"
       "h1,h2,h3,h4,h5,h6{margin-bottom:.25em;break-after:avoid;page-break-after:avoid}"
       "h1+*,h2+*,h3+*,h4+*,h5+*,h6+*{margin-top:0;break-before:avoid;page-break-before:avoid}"
       "h1+p,h2+p,h3+p,h4+p,h5+p,h6+p{orphans:1;widows:2}"
       "figure,.mini{break-inside:avoid;page-break-inside:avoid}"
       "figcaption{break-before:avoid;page-break-before:avoid}"
       "tr{break-inside:avoid;page-break-inside:avoid}")


def tls_context():
    """Return a verified TLS context augmented with common OS CA bundles."""
    context = ssl.create_default_context()
    candidates = (
        "/etc/ssl/cert.pem",                         # macOS
        "/etc/ssl/certs/ca-certificates.crt",       # Debian/Ubuntu
        "/etc/pki/tls/certs/ca-bundle.crt",         # Fedora/RHEL
        "/opt/homebrew/etc/ca-certificates/cert.pem",  # Homebrew
    )
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            try:
                context.load_verify_locations(cafile=str(path))
            except ssl.SSLError:
                continue
    return context


def cached_image(url, cache_dir):
    """Return a verified cached (bytes, media type) tuple, if available."""
    if cache_dir is None:
        return None
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    blob_path = Path(cache_dir) / f"{key}.bin"
    meta_path = Path(cache_dir) / f"{key}.json"
    try:
        blob = blob_path.read_bytes()
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        if (metadata.get("url") != url or metadata.get("size") != len(blob)
                or metadata.get("sha256") != hashlib.sha256(blob).hexdigest()
                or not metadata.get("content_type")):
            return None
        return blob, metadata["content_type"]
    except (OSError, ValueError, TypeError, KeyError):
        return None


def cache_image(url, blob, content_type, cache_dir):
    """Store a downloaded image and integrity metadata in the local cache."""
    if cache_dir is None:
        return
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    blob_path = cache_dir / f"{key}.bin"
    meta_path = cache_dir / f"{key}.json"
    metadata = {
        "url": url,
        "content_type": content_type,
        "size": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
    }
    with tempfile.NamedTemporaryFile(dir=cache_dir, prefix=f".{key}-", delete=False) as temp:
        temp.write(blob)
        temp_blob = Path(temp.name)
    try:
        temp_blob.replace(blob_path)
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=cache_dir,
                prefix=f".{key}-", delete=False) as temp:
            json.dump(metadata, temp, sort_keys=True)
            temp_meta = Path(temp.name)
        temp_meta.replace(meta_path)
    finally:
        temp_blob.unlink(missing_ok=True)
        if "temp_meta" in locals():
            temp_meta.unlink(missing_ok=True)


def clear_image_cache(cache_dir=IMAGE_CACHE_DIR):
    """Remove all cached remote images and return the number of image entries."""
    cache_dir = Path(cache_dir)
    count = len(list(cache_dir.glob("*.bin"))) if cache_dir.is_dir() else 0
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    return count


def download_image(url, context, attempts=4, cache_dir=None):
    """Return a cached image or download and cache it, retrying transient failures."""
    cached = cached_image(url, cache_dir)
    if cached is not None:
        return cached
    last_error = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "5epub/1.0", "Accept": "image/*", "Connection": "close"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30, context=context) as response:
                blob = response.read()
                content_length = response.headers.get("Content-Length")
                if not blob:
                    raise OSError("server returned an empty response")
                if content_length is not None and len(blob) != int(content_length):
                    raise OSError(
                        f"incomplete response ({len(blob)} of {content_length} bytes)"
                    )
                content_type = response.headers.get_content_type()
                cache_image(url, blob, content_type, cache_dir)
                return blob, content_type
        except (OSError, ValueError) as exc:
            last_error = exc
            status = getattr(exc, "code", None)
            if status is not None and 400 <= status < 500 and status not in (408, 429):
                break
            if attempt < attempts:
                delay = 0.5 * (2 ** (attempt - 1))
                print(
                    f"Image download failed (attempt {attempt}/{attempts}); "
                    f"retrying in {delay:g}s: {url}",
                    file=sys.stderr,
                )
                time.sleep(delay)
    raise OSError(f"failed after {attempts} attempts: {last_error}") from last_error


def slug(s):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(s)).strip("-") or "x"


def crop_cover_with_sips(blob, suffix):
    """Use native macOS tooling when Pillow is unavailable or cannot load."""
    executable = shutil.which("sips")
    if not executable:
        return None
    suffix = suffix if suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") else ".img"
    with tempfile.TemporaryDirectory(prefix="5epub-cover-") as td:
        source = Path(td) / ("source" + suffix)
        target = Path(td) / "cover.jpg"
        source.write_bytes(blob)
        probe = subprocess.run(
            [executable, "-g", "pixelWidth", "-g", "pixelHeight", str(source)],
            check=True, capture_output=True, text=True,
        )
        width_match = re.search(r"pixelWidth:\s*(\d+)", probe.stdout)
        height_match = re.search(r"pixelHeight:\s*(\d+)", probe.stdout)
        if not width_match or not height_match:
            return None
        width, height = int(width_match.group(1)), int(height_match.group(1))
        target_width = round(height * 8.5 / 11)
        if width - target_width < width * 0.05 or not 0.55 <= target_width / height <= 0.90:
            return blob, None
        subprocess.run(
            [executable, "--cropToHeightWidth", str(height), str(target_width),
             "-s", "format", "jpeg", str(source), "--out", str(target)],
            check=True, capture_output=True, text=True,
        )
        return target.read_bytes(), "image/jpeg"


def crop_cover_image(blob, suffix=".img"):
    """Crop uniform vertical padding from a cover and return JPEG/PNG bytes."""
    try:
        from PIL import Image, ImageStat
    except (ImportError, OSError) as exc:
        fallback = crop_cover_with_sips(blob, suffix)
        if fallback is not None:
            return fallback
        raise RuntimeError(
            "Cover cropping requires a working native Pillow installation. "
            "Reinstall Pillow for this Python architecture or use --no-crop-cover"
        ) from exc

    with Image.open(io.BytesIO(blob)) as source:
        source.load()
        image = source.convert("RGBA")
    width, height = image.size
    if width < 3 or height < 3:
        return blob, None

    alpha_box = image.getchannel("A").getbbox()
    if alpha_box and (alpha_box[0] > 0 or alpha_box[2] < width):
        left, right = alpha_box[0], alpha_box[2]
    else:
        left = right = None

    def edge_color(x):
        stats = ImageStat.Stat(image.crop((x, 0, x + 1, height)))
        return tuple(round(v) for v in stats.median[:3])

    def matching_fraction(x, color, tolerance=24):
        pixels = image.crop((x, 0, x + 1, height)).getdata()
        matches = sum(max(abs(px[i] - color[i]) for i in range(3)) <= tolerance for px in pixels)
        return matches / height

    if left is None:
        left_color, right_color = edge_color(0), edge_color(width - 1)
        left = 0
        while left < width // 3 and matching_fraction(left, left_color) >= 0.96:
            left += 1
        right = width
        while right > width * 2 // 3 and matching_fraction(right - 1, right_color) >= 0.96:
            right -= 1

    removed = width - (right - left)
    new_ratio = (right - left) / height
    if removed < width * 0.05 or not 0.55 <= new_ratio <= 0.90:
        return blob, None

    cropped = image.crop((left, 0, right, height))
    out = io.BytesIO()
    if cropped.getextrema()[3] != (255, 255):
        cropped.save(out, format="PNG", optimize=True)
        return out.getvalue(), "image/png"
    cropped.convert("RGB").save(out, format="JPEG", quality=92, optimize=True)
    return out.getvalue(), "image/jpeg"


class Converter(HTMLParser):
    """Turns loose HTML into well-formed XHTML, rewriting links and images."""

    def __init__(self, book):
        super().__init__(convert_charrefs=True)
        self.book, self.out, self.stack = book, [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "img" and attrs.get("src"):
            attrs["src"] = self.book.add_image(attrs["src"])
            attrs.setdefault("alt", "")
        if tag == "img" and attrs.get("srcset"):
            raise ValueError("Unpackaged resource attribute <img srcset> is not supported")
        for attr in DEPENDENCY_ATTRS.get(tag, ()):
            value = attrs.get(attr, "").strip()
            if value and not value.startswith(("data:", "#")):
                raise ValueError(f"Unpackaged resource attribute <{tag} {attr}> is not supported: {value}")
        if re.search(r"url\(", attrs.get("style", ""), re.IGNORECASE):
            raise ValueError(f"CSS resource URLs in style attributes are not supported on <{tag}>")
        if tag == "a" and attrs.get("href", "").startswith("#"):
            attrs["href"] = self.book.resolve(attrs["href"])
        a = "".join(f' {k}="{html.escape(v or "", quote=True)}"' for k, v in attrs.items())
        if tag in VOID:
            self.out.append(f"<{tag}{a}/>")
        else:
            self.stack.append(tag)
            self.out.append(f"<{tag}{a}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        self.out.append(html.escape(data, quote=False))

    def result(self):
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


class Book:
    def __init__(self, data, base, crop_cover=True, image_cache=None):
        self.data, self.base = data, base
        self.crop_cover = crop_cover
        self.image_cache = Path(image_cache) if image_cache is not None else None
        self.ssl_context = tls_context()
        self.images = {}  # source -> (zip name, bytes, media type)
        self.fonts = {}  # source -> (zip name, bytes, media type)
        self.sections = []  # dicts: id, file, title, kind
        used = set()
        for kind, key in (("chapter", "chapters"), ("appendix", "appendices")):
            for i, s in enumerate(data.get(key, []), 1):
                sid = slug(s.get("id") or f"{kind}{i}")
                while sid in used:
                    sid += "_"
                used.add(sid)
                self.sections.append({"id": sid, "file": f"{sid}.xhtml", "kind": kind,
                                      "title": s.get("title") or f"{kind.title()} {i}",
                                      "content": s.get("content", ""), "toc": s.get("toc", [])})
        self.by_id = {s["id"]: s for s in self.sections}
        self.by_id["index"] = {"file": "index.xhtml"}

    def resolve(self, href):
        sid, _, anchor = href.lstrip("#").partition("#")
        if sid in self.by_id:
            return self.by_id[sid]["file"] + (f"#{anchor}" if anchor else "")
        return href  # plain in-page anchor

    def add_image(self, src):
        if src in self.images:
            return "images/" + self.images[src][0]
        if src.startswith("data:"):
            try:
                header, payload = src[5:].split(",", 1)
                parts = header.split(";")
                ctype = parts[0] or "text/plain"
                if not ctype.startswith("image/"):
                    raise ValueError(f"data URI is not an image: {ctype}")
                blob = base64.b64decode(payload, validate=True) if "base64" in parts[1:] else unquote_to_bytes(payload)
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid image data URI") from exc
            name = f"{len(self.images) + 1}-image{mimetypes.guess_extension(ctype) or '.bin'}"
            self.images[src] = (name, blob, ctype)
            return "images/" + name
        fetch_src = "https:" + src if src.startswith("//") else src
        scheme = urlparse(fetch_src).scheme
        if scheme in ("http", "https"):
            try:
                blob, ctype = download_image(
                    fetch_src, self.ssl_context, cache_dir=self.image_cache
                )
            except OSError as exc:
                raise RuntimeError(f"Could not download required image {fetch_src!r}: {exc}") from exc
            name = Path(urlparse(fetch_src).path).name or "image"
        else:
            if scheme:
                raise ValueError(f"Unsupported image URL scheme {scheme!r}: {src}")
            p = (self.base / src)
            blob, name = p.read_bytes(), p.name
            ctype = mimetypes.guess_type(name)[0]
        ctype = ctype if ctype and ctype.startswith("image/") else (
            mimetypes.guess_type(name)[0] or "image/jpeg")
        if self.crop_cover and src == self.data.get("cover"):
            cropped_blob, cropped_type = crop_cover_image(blob, Path(name).suffix)
            if cropped_type:
                blob, ctype = cropped_blob, cropped_type
        name = f"{len(self.images) + 1}-{slug(Path(name).stem)}{mimetypes.guess_extension(ctype) or Path(name).suffix}"
        self.images[src] = (name, blob, ctype)
        return "images/" + name

    def add_font(self, src):
        if src in self.fonts:
            return "fonts/" + self.fonts[src][0]
        parsed = urlparse(src)
        if parsed.scheme:
            raise ValueError(f"Font sources must be local files, not URLs: {src}")
        path = Path(src)
        if not path.is_absolute():
            path = self.base / path
        blob = path.read_bytes()
        suffix = path.suffix.lower()
        ctype = {".otf": "font/otf", ".ttf": "font/ttf", ".woff": "font/woff",
                 ".woff2": "font/woff2"}.get(suffix)
        if ctype is None:
            raise ValueError(f"Unsupported font format: {path}")
        name = f"{len(self.fonts) + 1}-{slug(path.stem)}{suffix}"
        self.fonts[src] = (name, blob, ctype)
        return "fonts/" + name

    def body(self, content):
        if "<" not in content:
            content = "".join(f"<p>{html.escape(p.strip())}</p>"
                              for p in re.split(r"\n\s*\n", content) if p.strip())
        c = Converter(self)
        c.feed(content)
        c.close()
        result = c.result()
        if re.search(r"url\(", result, re.IGNORECASE):
            raise ValueError("CSS resource URLs in section content are not supported")
        return result

    def page(self, title, inner):
        lang = html.escape(str(self.data.get("language", "en")), quote=True)
        return ('<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
                f'lang="{lang}" xml:lang="{lang}"><head><meta charset="utf-8"/>'
                f'<title>{html.escape(title)}</title><link rel="stylesheet" href="style.css"/></head>'
                f'<body>{inner}</body></html>')

    def index_page(self):
        """Index entries: {term, term_to?, group?, targets: ["sid#anchor" | {"to": ..., "label": ...}]}."""
        entries = sorted(self.data.get("index", []), key=lambda e: (e.get("group", ""), e["term"].lower()))
        groups = {}
        for e in entries:
            groups.setdefault(e.get("group", ""), []).append(e)
        out = ["<h1>Index</h1>"]
        for g, items in groups.items():
            gid = slug(g or "all")
            if g:
                out.append(f'<h2 id="g-{gid}">{html.escape(g)}</h2>')
            letters = {}
            for e in items:
                ch = e["term"][:1].upper()
                letters.setdefault(ch if ch.isalpha() else "#", []).append(e)
            out.append('<p class="letters">' + " ".join(
                f'<a href="#g-{gid}-{slug(l)}">{html.escape(l)}</a>' for l in letters) + "</p>")
            for l, es in letters.items():
                out.append(f'<h3 id="g-{gid}-{slug(l)}">{html.escape(l)}</h3><ul class="index">')
                for e in es:
                    term = html.escape(e["term"])
                    if e.get("term_to"):
                        term = f'<a href="{html.escape(self.resolve("#" + e["term_to"]), quote=True)}"><b>{term}</b></a>'
                    links = []
                    for t in e.get("targets", []):
                        to, label = (t["to"], t.get("label")) if isinstance(t, dict) else (t, None)
                        sid = to.split("#")[0]
                        label = label or (self.by_id[sid].get("title", sid) if sid in self.by_id else to)
                        links.append(f'<a href="{html.escape(self.resolve("#" + to), quote=True)}">{html.escape(label)}</a>')
                    out.append(f"<li>{term}{': ' + ', '.join(links) if links else ''}</li>")
                out.append("</ul>")
        return self.page("Index", "".join(out))

    def nav_list(self):
        """Nested <ol>: each section, with its optional toc entries [(anchor, title, depth)] beneath."""
        def entry(href, title, kids):
            sub = ""
            if kids:
                sub = "<ol>" + "".join(kids) + "</ol>"
            return f'<li><a href="{href}">{html.escape(title)}</a>{sub}</li>'

        def nest(items, f):
            root, stack = [], []  # stack of (depth, children list)
            for anchor, title, depth in items:
                node = {"href": f"{f}#{anchor}" if anchor else f, "title": title, "kids": []}
                while stack and stack[-1][0] >= depth:
                    stack.pop()
                (stack[-1][1]["kids"] if stack else root).append(node)
                stack.append((depth, node))

            def render(n):
                return entry(n["href"], n["title"], [render(k) for k in n["kids"]])
            return [render(n) for n in root]

        parts = [entry(s["file"], s["title"], nest(s["toc"], s["file"])) for s in self.sections]
        if self.data.get("index"):
            parts.append(entry("index.xhtml", "Index", []))
        return "<ol>" + "".join(parts) + "</ol>"

    def build(self, out):
        d = self.data
        ident = d.get("identifier") or f"urn:uuid:{uuid.uuid4()}"
        pages = {s["file"]: self.page(s["title"], f'<h1>{html.escape(s["title"])}</h1>' + self.body(s["content"]))
                 for s in self.sections}
        has_index = bool(d.get("index"))
        if has_index:
            pages["index.xhtml"] = self.index_page()
        cover = None
        if d.get("cover"):
            cover = self.add_image(d["cover"])
        font_faces = []
        for font in d.get("fonts", []):
            href = self.add_font(font["src"])
            family = str(font["family"]).replace("\\", "\\\\").replace('"', '\\"')
            weight = font.get("weight", "normal")
            style = font.get("style", "normal")
            font_format = {".otf": "opentype", ".ttf": "truetype", ".woff": "woff",
                           ".woff2": "woff2"}[Path(href).suffix.lower()]
            font_faces.append(
                f'@font-face{{font-family:"{family}";src:url("{href}") format("{font_format}");'
                f'font-weight:{weight};font-style:{style}}}'
            )
        remote_pages = [name for name, page in pages.items() if REMOTE_RESOURCE_RE.search(page)]
        if remote_pages:
            raise ValueError("EPUB pages contain unpackaged remote resources: " + ", ".join(remote_pages))
        nav_items = [(s["file"], s["title"]) for s in self.sections] + ([("index.xhtml", "Index")] if has_index else [])
        nav = self.page("Contents", '<nav epub:type="toc" id="toc"><h1>Contents</h1>' + self.nav_list() + "</nav>")

        manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                    '<item id="css" href="style.css" media-type="text/css"/>']
        spine = []
        for i, (f, _) in enumerate(nav_items):
            manifest.append(f'<item id="p{i}" href="{f}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="p{i}"/>')
        for i, (name, _, ctype) in enumerate(self.images.values()):
            props = ' properties="cover-image"' if cover == "images/" + name else ""
            manifest.append(f'<item id="img{i}" href="images/{name}" media-type="{ctype}"{props}/>')
        for i, (name, _, ctype) in enumerate(self.fonts.values()):
            manifest.append(f'<item id="font{i}" href="fonts/{name}" media-type="{ctype}"/>')
        opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
               '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
               f'<dc:identifier id="uid">{html.escape(ident)}</dc:identifier>'
               f'<dc:title>{html.escape(d.get("title", "Untitled"))}</dc:title>'
               f'<dc:language>{html.escape(str(d.get("language", "en")))}</dc:language>'
               + (f'<dc:creator>{html.escape(d["author"])}</dc:creator>' if d.get("author") else "")
               + f'<meta property="dcterms:modified">{__import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</meta>'
               f'</metadata><manifest>{"".join(manifest)}</manifest><spine>{"".join(spine)}</spine></package>')
        container = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                     '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                     '</rootfiles></container>')
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
            stylesheet = "".join(font_faces) + CSS + d.get("css", "")
            for name, text in {"content.opf": opf, "nav.xhtml": nav, "style.css": stylesheet, **pages}.items():
                z.writestr(f"OEBPS/{name}", text, compress_type=zipfile.ZIP_DEFLATED)
            for name, blob, _ in self.images.values():
                z.writestr(f"OEBPS/images/{name}", blob, compress_type=zipfile.ZIP_DEFLATED)
            for name, blob, _ in self.fonts.values():
                z.writestr(f"OEBPS/fonts/{name}", blob, compress_type=zipfile.ZIP_DEFLATED)


def main():
    ap = argparse.ArgumentParser(description="Make an EPUB from JSON.")
    ap.add_argument("json", nargs="?")
    ap.add_argument("-o", "--output")
    ap.add_argument("--no-crop-cover", action="store_true", help="preserve the original cover canvas")
    ap.add_argument("--clear-cache", action="store_true", help="clear cached web images before continuing")
    a = ap.parse_args()
    if a.clear_cache:
        count = clear_image_cache(IMAGE_CACHE_DIR)
        print(f"Cleared {count} cached image{'s' if count != 1 else ''} from {IMAGE_CACHE_DIR}")
    if not a.json:
        if a.clear_cache:
            return
        ap.error("json is required (unless using --clear-cache)")
    src = Path(a.json)
    data = json.loads(src.read_text(encoding="utf-8"))
    out = a.output or str(src.with_suffix(".epub"))
    Book(data, src.parent, crop_cover=not a.no_crop_cover,
         image_cache=IMAGE_CACHE_DIR).build(out)
    print(f"Wrote {out}")


if __name__ == "__main__":
    sys.exit(main())
