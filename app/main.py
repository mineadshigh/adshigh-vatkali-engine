import hashlib
import os
import time
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import httpx
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright

APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
FEED_URL = os.getenv("FEED_URL", "https://www.vatkali.com/Xml/?Type=FACEBOOK&fname=vatkali")

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # /srv/app
STATIC_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frameassets"))  # /srv/frameassets
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# --- HTTP client (reuse) ---
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome Safari",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": "https://www.vatkali.com/",
    "Origin": "https://www.vatkali.com",
}
http_client = httpx.Client(follow_redirects=True, timeout=25, headers=HTTP_HEADERS)

# --- tiny in-memory cache to reduce repeated downloads ---
_IMG_CACHE = {}  # url -> (ts, content_type, bytes)
CACHE_TTL_SECONDS = 10 * 60  # 10 dk


def _cache_get(url: str):
    hit = _IMG_CACHE.get(url)
    if not hit:
        return None
    ts, ct, data = hit
    if (time.time() - ts) > CACHE_TTL_SECONDS:
        _IMG_CACHE.pop(url, None)
        return None
    return ct, data


def _cache_set(url: str, content_type: str, data: bytes):
    # basit limit
    if len(_IMG_CACHE) > 400:
        _IMG_CACHE.clear()
    _IMG_CACHE[url] = (time.time(), content_type, data)


def get_base_url(request: Request) -> str:
    return APP_BASE_URL if APP_BASE_URL else str(request.base_url).rstrip("/")


def norm_price(s: str) -> str:
    return " ".join((s or "").split()).strip()


def format_price(s: str) -> str:
    """Feed TRY veriyorsa TL’ye çevir."""
    x = norm_price(s)
    # ör: "476,00 TRY" => "476,00 TL"
    x = x.replace(" TRY", " TL").replace("TRY", "TL")
    return x


def hidden_flags(price: str, sale: str):
    p = norm_price(price)
    s = norm_price(sale)
    if (not s) or (s == p):
        return ("hidden", "hidden", "")  # old_hidden, new_hidden, single_hidden
    return ("", "", "hidden")


def build_sig(*parts: str) -> str:
    raw = "|".join([p or "" for p in parts]).encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:12]


def choose_images(item: ET.Element):
    ns = {"g": "http://base.google.com/ns/1.0"}
    primary = (item.findtext("g:image_link", default="", namespaces=ns) or "").strip()

    additional = [
        (e.text or "").strip()
        for e in item.findall("g:additional_image_link", namespaces=ns)
        if e is not None and (e.text or "").strip()
    ]

    s1 = additional[0] if len(additional) >= 1 else primary
    s2 = additional[1] if len(additional) >= 2 else (additional[0] if len(additional) >= 1 else primary)
    return primary, s1, s2


def wait_images(page):
    page.evaluate(
        """() => Promise.all(Array.from(document.images).map(img => {
            if (img.complete) return Promise.resolve();
            return new Promise(res => { img.onload = img.onerror = () => res(); });
        }))"""
    )


def render_png(html: str, width=1080, height=1080) -> bytes:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": width, "height": height, "deviceScaleFactor": 2})

        page.set_content(html, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=20000)
        wait_images(page)
        page.wait_for_timeout(120)

        buf = page.screenshot(type="png", full_page=False)
        browser.close()
        return buf


def img_proxy_url(request: Request, remote_url: str) -> str:
    """Remote URL -> bizim domainimizde /img?url=..."""
    if not remote_url:
        return ""
    base_url = get_base_url(request)
    return f"{base_url}/img?url={quote_plus(remote_url)}"


@app.get("/img")
def img(url: str = Query(...)):
    """
    Remote image proxy:
    - Playwright dış domainde hotlink/headers yüzünden bazen görsel çekemiyor.
    - Bu endpoint aynı-origin yapar, stabil olur.
    """
    cached = _cache_get(url)
    if cached:
        ct, data = cached
        return Response(content=data, media_type=ct)

    r = http_client.get(url)
    r.raise_for_status()
    ct = (r.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    data = r.content
    _cache_set(url, ct, data)

    # cache-control eklemek istersen:
    return Response(
        content=data,
        media_type=ct,
        headers={"Cache-Control": "public, max-age=600"},
    )


@app.get("/render.png")
def render_endpoint(
    request: Request,
    title: str = Query(""),
    price: str = Query(""),
    sale_price: str = Query(""),
    product_image_primary: str = Query(""),
    product_image_secondary_1: str = Query(""),
    product_image_secondary_2: str = Query(""),
    logo_url: str = Query(""),
):
    old_hidden, new_hidden, single_hidden = hidden_flags(price, sale_price)

    # TRY->TL
    price = format_price(price)
    sale_price = format_price(sale_price)

    template_path = os.path.join(BASE_DIR, "template.html")
    css_path = os.path.join(BASE_DIR, "styles.css")

    with open(template_path, "r", encoding="utf-8") as f:
        tpl = f.read()
    with open(css_path, "r", encoding="utf-8") as f:
        css = f.read()

    if not logo_url:
        base_url = get_base_url(request)
        logo_url = f"{base_url}/static/vatkalilogo.svg"

    # ✅ Remote görselleri /img üzerinden same-origin yap
    product_image_primary = img_proxy_url(request, product_image_primary)
    product_image_secondary_1 = img_proxy_url(request, product_image_secondary_1)
    product_image_secondary_2 = img_proxy_url(request, product_image_secondary_2)
    logo_url = img_proxy_url(request, logo_url) if logo_url.startswith("http") else logo_url

    html = tpl.replace("{{CSS}}", css)
    html = html.replace("{{product_image_primary}}", product_image_primary)
    html = html.replace("{{product_image_secondary_1}}", product_image_secondary_1)
    html = html.replace("{{product_image_secondary_2}}", product_image_secondary_2)
    html = html.replace("{{logo_url}}", logo_url)
    html = html.replace("{{title}}", title)
    html = html.replace("{{price}}", price)
    html = html.replace("{{sale_price}}", sale_price)
    html = html.replace("{{old_hidden}}", old_hidden)
    html = html.replace("{{new_hidden}}", new_hidden)
    html = html.replace("{{single_hidden}}", single_hidden)

    png = render_png(html, width=1080, height=1080)
    return Response(content=png, media_type="image/png")


@app.get("/feed.xml", response_class=PlainTextResponse)
def feed_proxy(request: Request, limit: int = 10):
    base_url = get_base_url(request)

    r = http_client.get(FEED_URL)
    r.raise_for_status()

    root = ET.fromstring(r.text)
    channel = root.find("channel")
    if channel is None:
        return PlainTextResponse(r.text, media_type="application/xml")

    items = channel.findall("item")
    items = items[: max(1, min(limit, len(items)))]

    ns = {"g": "http://base.google.com/ns/1.0"}

    for item in items:
        title = (item.findtext("title") or "").strip()
        price = (item.findtext("g:price", default="", namespaces=ns) or "").strip()
        sale = (item.findtext("g:sale_price", default="", namespaces=ns) or "").strip()

        primary, s1, s2 = choose_images(item)
        sig = build_sig(title, price, sale, primary, s1, s2)

        render_url = (
            f"{base_url}/render.png"
            f"?title={quote_plus(title)}"
            f"&price={quote_plus(price)}"
            f"&sale_price={quote_plus(sale)}"
            f"&product_image_primary={quote_plus(primary)}"
            f"&product_image_secondary_1={quote_plus(s1)}"
            f"&product_image_secondary_2={quote_plus(s2)}"
            f"&v={sig}"
        )

        img_tag = item.find("g:image_link", ns)
        if img_tag is None:
            img_tag = ET.SubElement(item, "{http://base.google.com/ns/1.0}image_link")
        img_tag.text = render_url

    xml_out = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    return PlainTextResponse(xml_out, media_type="application/xml")


@app.get("/probe")
def probe(url: str = Query(...)):
    try:
        r = http_client.get(url)
        content_type = r.headers.get("content-type", "")
        is_text = ("text" in content_type) or ("html" in content_type)
        return {
            "url": url,
            "status_code": r.status_code,
            "content_type": content_type,
            "content_length": len(r.content),
            "text_preview": r.text[:300] if is_text else None,
        }
    except Exception as e:
        return {"url": url, "error": str(e)}
