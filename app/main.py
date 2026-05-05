import asyncio
import base64
import hashlib
import os
import re
from urllib.parse import quote_plus, urlsplit, urlunsplit, parse_qsl, urlencode
from xml.etree import ElementTree as ET

import httpx
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright

APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")

FEED_URL_META = os.getenv(
    "FEED_URL_META",
    "https://www.vatkali.com/Xml/?Type=FACEBOOK&fname=vatkali",
)
FEED_URL_TIKTOK = os.getenv(
    "FEED_URL_TIKTOK",
    "https://www.vatkali.com/feed/tiktokfeed.xml",
)

RENDER_CONCURRENCY = int(os.getenv("RENDER_CONCURRENCY", "4"))
_render_sem = asyncio.Semaphore(RENDER_CONCURRENCY)

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frameassets"))
CACHE_DIR = os.path.join(BASE_DIR, "render_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# -------------------------
# Helpers
# -------------------------

def norm_price(s: str) -> str:
    return " ".join((s or "").split()).strip()

def format_currency_tr(s: str) -> str:
    x = norm_price(s)
    if not x:
        return x
    return x.replace("TRY", "TL").replace("try", "TL")

def _clean_url(u: str) -> str:
    if not u:
        return ""
    parts = urlsplit(u)
    q = [
        (k, v)
        for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower().startswith("utm_") or k.lower() in {"fbclid", "gclid"})
    ]
    new_query = urlencode(q, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))

def get_base_url(request: Request) -> str:
    return APP_BASE_URL if APP_BASE_URL else str(request.base_url).rstrip("/")

def _parse_money_to_float(s: str) -> float | None:
    if not s:
        return None

    t = str(s).strip()
    t = re.sub(r"[^\d.,]", "", t)
    if not t:
        return None

    if "." in t and "," in t:
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "")
            t = t.replace(",", ".")
        else:
            t = t.replace(",", "")
    else:
        if "," in t and "." not in t:
            t = t.replace(",", ".")
        elif "." in t and "," not in t:
            if re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
                t = t.replace(".", "")

    try:
        return float(t)
    except Exception:
        return None

def format_tl(price: str) -> str:
    v = _parse_money_to_float(price)
    if v is None:
        return format_currency_tr(price)
    n = int(round(v))
    return f"{n:,}".replace(",", ".") + " TL"

def hidden_flags(price: str, sale: str):
    p = _parse_money_to_float(price)
    s = _parse_money_to_float(sale)

    if p is None or s is None:
        return ("hidden", "hidden", "")

    if abs(p - s) < 0.005 or s >= p:
        return ("hidden", "hidden", "")

    return ("", "", "hidden")

def build_sig(*parts: str) -> str:
    raw = "|".join([p or "" for p in parts]).encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:12]

def build_render_cache_key(
    title: str,
    price: str,
    sale_price: str,
    product_image_primary: str,
    product_image_secondary_1: str,
    logo_url: str,
    design: str,
    w: int,
    h: int,
) -> str:
    return build_sig(
        title,
        price,
        sale_price,
        product_image_primary,
        product_image_secondary_1,
        logo_url,
        design,
        str(w),
        str(h),
    )

def get_cache_file_path(cache_key: str) -> str:
    return os.path.join(CACHE_DIR, f"{cache_key}.png")

def get_template_and_css(design: str) -> tuple[str, str]:
    if design == "meta_summer26":
        return (
            os.path.join(BASE_DIR, "template_meta_summer26.html"),
            os.path.join(BASE_DIR, "styles_meta_summer26.css"),
        )

    if design == "tiktok_summer26":
        return (
            os.path.join(BASE_DIR, "template_tiktok_summer26.html"),
            os.path.join(BASE_DIR, "styles_tiktok_summer26.css"),
        )

    if design == "tiktok_v1":
        return (
            os.path.join(BASE_DIR, "template_tiktok.html"),
            os.path.join(BASE_DIR, "styles_tiktok.css"),
        )

    return (
        os.path.join(BASE_DIR, "template_meta.html"),
        os.path.join(BASE_DIR, "styles_meta.css"),
    )

# -------------------------
# XML utilities
# -------------------------

def text_of(item: ET.Element, tag: str, ns: dict | None = None) -> str:
    if ns and ":" in tag:
        return (item.findtext(tag, default="", namespaces=ns) or "").strip()
    return (item.findtext(tag, default="") or "").strip()

def set_image_link(item: ET.Element, new_url: str):
    ns = {"g": "http://base.google.com/ns/1.0"}

    img = item.find("g:image_link", ns)
    if img is not None:
        img.text = new_url
        for extra in item.findall("g:additional_image_link", ns):
            item.remove(extra)
        return

    img = item.find("image_link")
    if img is not None:
        img.text = new_url
        for extra in item.findall("additional_image_link"):
            item.remove(extra)
        return

    ET.SubElement(item, "image_link").text = new_url

def extract_title(item: ET.Element, ns: dict | None = None) -> str:
    candidates = [
        text_of(item, "title", ns),
        text_of(item, "g:title", ns),
        text_of(item, "name", ns),
        text_of(item, "product_name", ns),
        text_of(item, "product_title", ns),
        text_of(item, "item_name", ns),
        text_of(item, "description", ns),
        text_of(item, "g:description", ns),
    ]

    for c in candidates:
        if c:
            return " ".join(c.split()).strip()

    return ""

def get_custom_labels(item: ET.Element, ns: dict | None = None) -> str:
    labels = []

    for i in range(5):
        labels.append(text_of(item, f"g:custom_label_{i}", ns))
        labels.append(text_of(item, f"custom_label_{i}", ns))

    return " ".join([x for x in labels if x]).lower()

# -------------------------
# Image selection
# -------------------------

def choose_images_any(item: ET.Element):
    ns = {"g": "http://base.google.com/ns/1.0"}

    primary_raw = text_of(item, "g:image_link", ns=ns) or text_of(item, "image_link")
    additional_raw = []

    for e in item.findall("g:additional_image_link", namespaces=ns):
        if e is not None and (e.text or "").strip():
            additional_raw.append((e.text or "").strip())

    for e in item.findall("additional_image_link"):
        if e is not None and (e.text or "").strip():
            additional_raw.append((e.text or "").strip())

    all_urls = [primary_raw] + additional_raw

    seen = set()
    uniq = []
    for u in all_urls:
        cu = _clean_url(u)
        if cu and cu not in seen:
            seen.add(cu)
            uniq.append(u)

    primary = uniq[0] if uniq else primary_raw
    s1 = uniq[1] if len(uniq) > 1 else primary

    return primary, s1

# -------------------------
# HTTP -> Data URI
# -------------------------

_TRANSPARENT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)

def _transparent_data_uri() -> str:
    return "data:image/png;base64," + base64.b64encode(_TRANSPARENT_PNG).decode("ascii")

def _guess_mime(url: str, content_type: str | None) -> str:
    if content_type and "image/" in content_type:
        return content_type.split(";")[0].strip()
    u = (url or "").lower()
    if ".png" in u:
        return "image/png"
    if ".webp" in u:
        return "image/webp"
    if ".svg" in u:
        return "image/svg+xml"
    return "image/jpeg"

async def to_data_uri(url: str, client: httpx.AsyncClient) -> str:
    if not url:
        return _transparent_data_uri()
    if url.startswith("data:"):
        return url

    cleaned_url = _clean_url(url)

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome Safari",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "Referer": "https://www.vatkali.com/",
    }

    try:
        r = await client.get(
            cleaned_url,
            headers=headers,
            timeout=30.0,
            follow_redirects=True,
        )
        r.raise_for_status()

        ct = (r.headers.get("content-type") or "").lower()

        # ❗ image değilse direkt URL dön
        if "image/" not in ct:
            return cleaned_url

        # ❗ çok büyükse base64 yapma (performans + timeout)
        if len(r.content) > 8_000_000:
            return cleaned_url

        mime = _guess_mime(cleaned_url, r.headers.get("content-type"))
        b64 = base64.b64encode(r.content).decode("ascii")
        return f"data:{mime};base64,{b64}"

    except Exception:
        # 🔥 EN KRİTİK FIX
        # eskiden transparan png dönüyordu → şimdi URL fallback
        return cleaned_url

# -------------------------
# Playwright
# -------------------------

_pw = None
_browser = None
_pw_lock = asyncio.Lock()

def _is_fatal_playwright_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(
        s in msg
        for s in [
            "targetclosederror",
            "target page",
            "has been closed",
            "writeunixtransport closed",
            "handler is closed",
            "playwright connection closed",
        ]
    )

async def _restart_playwright():
    global _pw, _browser

    try:
        if _browser:
            await _browser.close()
    except Exception:
        pass
    _browser = None

    try:
        if _pw:
            await _pw.stop()
    except Exception:
        pass
    _pw = None

    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--no-zygote",
            "--disable-gpu",
        ],
    )

async def _ensure_browser():
    global _pw, _browser

    async with _pw_lock:
        try:
            if _pw is None:
                _pw = await async_playwright().start()

            if _browser is None or (hasattr(_browser, "is_connected") and not _browser.is_connected()):
                try:
                    if _browser:
                        await _browser.close()
                except Exception:
                    pass

                _browser = await _pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--no-zygote",
                        "--disable-gpu",
                    ],
                )
        except Exception as e:
            if _is_fatal_playwright_error(e):
                await _restart_playwright()
            else:
                raise

@app.on_event("startup")
async def _startup():
    await _ensure_browser()

@app.on_event("shutdown")
async def _shutdown():
    global _pw, _browser

    try:
        if _browser:
            await _browser.close()
    except Exception:
        pass
    _browser = None

    try:
        if _pw:
            await _pw.stop()
    except Exception:
        pass
    _pw = None

async def render_png(html: str, width=1080, height=1080) -> bytes:
    global _browser

    async with _render_sem:
        await _ensure_browser()

        async def _do() -> bytes:
            page = await _browser.new_page(viewport={"width": width, "height": height})
            try:
                await page.set_content(html, wait_until="domcontentloaded")
                await page.wait_for_timeout(120)
                
                ok = await page.locator(".product-image").evaluate(

                    """img => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0"""

                )

                if not ok:
                    raise Exception("PRODUCT_IMAGE_NOT_LOADED")
                    
                frame = page.locator(".frame")
                await frame.wait_for(state="visible", timeout=5000)
                
                return await frame.screenshot(type="png")
                
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

        try:
            return await _do()
        except Exception as e:
            if _is_fatal_playwright_error(e):
                await _restart_playwright()
                return await _do()
            raise

# -------------------------
# Endpoints
# -------------------------

@app.get("/render.png")
async def render_endpoint(
    request: Request,
    title: str = Query(""),
    price: str = Query(""),
    sale_price: str = Query(""),
    product_image_primary: str = Query(""),
    product_image_secondary_1: str = Query(""),
    logo_url: str = Query(""),
    design: str = Query("meta_v1"),
    w: int = Query(1080),
    h: int = Query(1080),
):
    price = format_tl(price)
    sale_price = format_tl(sale_price)

    old_hidden, new_hidden, single_hidden = hidden_flags(price, sale_price)

    template_path, css_path = get_template_and_css(design)

    with open(template_path, "r", encoding="utf-8") as f:
        tpl = f.read()

    with open(css_path, "r", encoding="utf-8") as f:
        css = f.read()

    if not logo_url:
        base_url = get_base_url(request)
        logo_url = f"{base_url}/static/vatkalilogo.svg"

    secondary_for_cache = product_image_secondary_1 if design == "meta_v1" else ""

    cache_key = build_render_cache_key(
        title=title,
        price=price,
        sale_price=sale_price,
        product_image_primary=product_image_primary,
        product_image_secondary_1=secondary_for_cache,
        logo_url=logo_url,
        design=design,
        w=w,
        h=h,
    )
    cache_file = get_cache_file_path(cache_key)

    if os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            png = f.read()
        headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        return Response(content=png, media_type="image/png", headers=headers)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        if design == "meta_v1":
            product_image_primary_data, product_image_secondary_1_data, logo_data = await asyncio.gather(
                to_data_uri(product_image_primary, client),
                to_data_uri(product_image_secondary_1, client),
                to_data_uri(logo_url, client),
            )
        else:
            product_image_primary_data, logo_data = await asyncio.gather(
                to_data_uri(product_image_primary, client),
                to_data_uri(logo_url, client),
            )
            product_image_secondary_1_data = ""

    html = tpl.replace("{{CSS}}", css)
    html = html.replace("{{product_image_primary}}", product_image_primary_data)
    html = html.replace("{{product_image_secondary_1}}", product_image_secondary_1_data)
    html = html.replace("{{logo_url}}", logo_data)
    html = html.replace("{{title}}", title)
    html = html.replace("{{price}}", price)
    html = html.replace("{{sale_price}}", sale_price)
    html = html.replace("{{old_hidden}}", old_hidden)
    html = html.replace("{{new_hidden}}", new_hidden)
    html = html.replace("{{single_hidden}}", single_hidden)

    try:
        png = await render_png(html, width=w, height=h)
        with open(cache_file, "wb") as f:
            f.write(png)
    except Exception as e:
        print("RENDER_FAILED:", repr(e))
        return Response(status_code=500)
    

    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    return Response(content=png, media_type="image/png", headers=headers)

@app.get("/feed_meta.xml", response_class=PlainTextResponse)
async def feed_meta(request: Request):
    base_url = get_base_url(request)
    fv = (request.query_params.get("v") or "").strip()

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.get(FEED_URL_META)
        r.raise_for_status()

    root = ET.fromstring(r.text)
    channel = root.find("channel")
    if channel is None:
        return PlainTextResponse(r.text, media_type="application/xml")

    items = channel.findall("item")
    ns = {"g": "http://base.google.com/ns/1.0"}

    for item in items:
        title = extract_title(item, ns)
        price = format_currency_tr(item.findtext("g:price", default="", namespaces=ns) or "")
        sale = format_currency_tr(item.findtext("g:sale_price", default="", namespaces=ns) or "")
        if not sale:
            sale = price

        primary, s1 = choose_images_any(item)

        custom_labels = (
            get_custom_labels(item, ns)
            .replace("`", "'")
            .replace("’", "'")
        )

        if "summer'26" in custom_labels or "summer26" in custom_labels or "summer 26" in custom_labels:
            design = "meta_summer26"
        else:
            design = "meta_v1"

        sig = build_sig(design, title, price, sale, primary, s1, fv)

        render_url = (
            f"{base_url}/render.png"
            f"?title={quote_plus(title)}"
            f"&price={quote_plus(price)}"
            f"&sale_price={quote_plus(sale)}"
            f"&product_image_primary={quote_plus(primary)}"
            f"&product_image_secondary_1={quote_plus(s1)}"
            f"&design={quote_plus(design)}"
            f"&w=1080&h=1080"
            f"&fv={quote_plus(fv)}"
            f"&v={sig}"
        )

        set_image_link(item, render_url)

    xml_out = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    return PlainTextResponse(xml_out, media_type="application/xml", headers=headers)

@app.get("/feed.xml", response_class=PlainTextResponse)
async def feed_legacy(request: Request):
    return await feed_meta(request)

@app.get("/feed_tiktok.xml", response_class=PlainTextResponse)
async def feed_tiktok(request: Request):
    base_url = get_base_url(request)
    fv = (request.query_params.get("v") or "").strip()

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.get(FEED_URL_TIKTOK)
        r.raise_for_status()

    root = ET.fromstring(r.text)
    channel = root.find("channel")
    if channel is None:
        return PlainTextResponse(r.text, media_type="application/xml")

    items = channel.findall("item")
    ns = {"g": "http://base.google.com/ns/1.0"}

    for item in items:
        title = extract_title(item, ns)
        price = format_currency_tr(
            item.findtext("g:price", default="", namespaces=ns)
            or item.findtext("price")
            or ""
        )
        sale = format_currency_tr(
            item.findtext("g:sale_price", default="", namespaces=ns)
            or item.findtext("sale_price")
            or ""
        )
        if not sale:
            sale = price

        primary, _ = choose_images_any(item)

        custom_labels = (
            get_custom_labels(item, ns)
            .replace("`", "'")
            .replace("’", "'")
        )

        if "summer'26" in custom_labels or "summer26" in custom_labels or "summer 26" in custom_labels:
            design = "tiktok_summer26"
        else:
            design = "tiktok_v1"

        sig = build_sig(design, title, price, sale, primary, fv)

        render_url = (
            f"{base_url}/render.png"
            f"?title={quote_plus(title)}"
            f"&price={quote_plus(price)}"
            f"&sale_price={quote_plus(sale)}"
            f"&product_image_primary={quote_plus(primary)}"
            f"&design={quote_plus(design)}"
            f"&w=1080&h=1920"
            f"&fv={quote_plus(fv)}"
            f"&v={sig}"
        )

        set_image_link(item, render_url)

    xml_out = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    return PlainTextResponse(xml_out, media_type="application/xml", headers=headers)

@app.get("/probe")
async def probe(url: str = Query(...)):
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome Safari",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers=headers) as client:
            r = await client.get(url)
            content_type = r.headers.get("content-type", "")
            is_text = ("text" in content_type) or ("html" in content_type)

            return {
                "url": url,
                "status_code": r.status_code,
                "content_type": content_type,
                "content_length": len(r.content),
                "first_50_bytes_base64": base64.b64encode(r.content[:50]).decode("ascii"),
                "text_preview": r.text[:300] if is_text else None,
            }
    except Exception as e:
        return {"url": url, "error": str(e)}
