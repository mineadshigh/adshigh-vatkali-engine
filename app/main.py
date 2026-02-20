import asyncio
import base64
import hashlib
import os
import re
import unicodedata
from urllib.parse import quote_plus, urlsplit, urlunsplit, parse_qsl, urlencode
from xml.etree import ElementTree as ET

import httpx
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright

APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
FEED_URL = os.getenv("FEED_URL", "https://www.vatkali.com/Xml/?Type=FACEBOOK&fname=vatkali")

# 1GB RAM ortamda güvenli default: 1
RENDER_CONCURRENCY = int(os.getenv("RENDER_CONCURRENCY", "1"))
_render_sem = asyncio.Semaphore(RENDER_CONCURRENCY)

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # /srv/app
STATIC_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frameassets"))  # /srv/frameassets
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
    """fbclid/utm gibi takip parametrelerini temizle."""
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


def choose_images(item: ET.Element):
    ns = {"g": "http://base.google.com/ns/1.0"}

    primary_raw = (item.findtext("g:image_link", default="", namespaces=ns) or "").strip()

    additional_raw = [
        (e.text or "").strip()
        for e in item.findall("g:additional_image_link", namespaces=ns)
        if e is not None and (e.text or "").strip()
    ]

    all_urls = [primary_raw] + additional_raw

    seen = set()
    uniq = []
    for u in all_urls:
        cu = _clean_url(u)
        if cu and cu not in seen:
            seen.add(cu)
            uniq.append(u)  # orijinal URL

    primary = uniq[0] if uniq else primary_raw
    s1 = uniq[1] if len(uniq) > 1 else ""
    s2 = uniq[2] if len(uniq) > 2 else ""

    if not s1:
        s1 = primary
    if not s2:
        s2 = s1

    return primary, s1, s2


def hidden_flags(price: str, sale: str):
    p = norm_price(price)
    s = norm_price(sale)
    if (not s) or (s == p):
        return ("hidden", "hidden", "")
    return ("", "", "hidden")


def build_sig(*parts: str) -> str:
    raw = "|".join([p or "" for p in parts]).encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:12]


def get_base_url(request: Request) -> str:
    return APP_BASE_URL if APP_BASE_URL else str(request.base_url).rstrip("/")


def tr_title_case(text: str) -> str:
    """Her kelimenin baş harfi büyük (TR i/ı uyumlu)."""
    text = (text or "").strip()
    if not text:
        return ""

    def cap_word(w: str) -> str:
        if not w:
            return w
        first = w[0]
        rest = w[1:]

        if first == "i":
            first_up = "İ"
        elif first == "ı":
            first_up = "I"
        else:
            first_up = first.upper()

        rest_low = rest.lower()
        rest_low = rest_low.replace("I", "ı").replace("İ", "i")
        return first_up + rest_low

    parts = re.split(r"(\s+)", text)
    return "".join([cap_word(p) if not p.isspace() else p for p in parts])


def _parse_money_to_float(s: str) -> float | None:
    if not s:
        return None
    t = s.strip()
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

    try:
        return float(t)
    except Exception:
        return None


def calc_discount_percent(price_str: str, sale_str: str) -> int | None:
    p = _parse_money_to_float(price_str)
    s = _parse_money_to_float(sale_str)
    if not p or not s or p <= 0 or s >= p:
        return None
    pct = int(round((1 - (s / p)) * 100))
    if pct <= 0:
        return None
    return pct


# -------------------------
# SEASON RULE (custom_label_1 -> theme)  ✅ ONLY "İlkbahar-Yaz 26"
# -------------------------

_HYPHENS = {
    "\u2010",  # hyphen
    "\u2011",  # non-breaking hyphen
    "\u2012",  # figure dash
    "\u2013",  # en dash
    "\u2014",  # em dash
    "\u2212",  # minus sign
    "\u00ad",  # soft hyphen
}

def _norm_season_text(s: str) -> str:
    """
    Unicode farkları (tire, NBSP, vb.) yüzünden eşleşme kaçmasın diye normalize.
    """
    if not s:
        return ""
    x = unicodedata.normalize("NFKC", s)
    for h in _HYPHENS:
        x = x.replace(h, "-")
    x = x.replace("\u00a0", " ")
    x = " ".join(x.split()).strip()
    return x.lower()

# ✅ Sadece bunu season sayıyoruz:
_ONLY_SEASON_TOKEN_NORM = _norm_season_text("İlkbahar-Yaz 26")

def is_season_label(label_value: str) -> bool:
    """
    Kural: custom_label_1 içinde SADECE "İlkbahar-Yaz 26" geçiyorsa season.
    Boşsa / başka sezonlar varsa classic.
    """
    v = _norm_season_text(label_value)
    if not v:
        return False
    return _ONLY_SEASON_TOKEN_NORM in v

def find_text_by_localname(item: ET.Element, local_name: str) -> str:
    """
    Namespace prefix'e takılmadan (g:, ns0: vs) custom_label_1 gibi alanları bul.
    """
    if item is None:
        return ""
    for el in item.iter():
        tag = el.tag
        if isinstance(tag, str):
            ln = tag.split("}")[-1]  # "{uri}custom_label_1" -> "custom_label_1"
            if ln == local_name:
                return (el.text or "").strip()
    return ""


_TRANSPARENT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)


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


# -------------------------
# HTTP -> Data URI (robust)
# -------------------------

async def to_data_uri(url: str, client: httpx.AsyncClient) -> str:
    if not url:
        return "data:image/png;base64," + base64.b64encode(_TRANSPARENT_PNG).decode("ascii")

    if url.startswith("data:"):
        return url

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome Safari",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "Referer": "https://www.vatkali.com/",
        "Origin": "https://www.vatkali.com",
    }

    try:
        # kısa timeout: Meta bot taramasında takılmasın
        r = await client.get(url, headers=headers, timeout=15.0)
        r.raise_for_status()

        ct = (r.headers.get("content-type") or "").lower()
        if "image/" not in ct:
            return "data:image/png;base64," + base64.b64encode(_TRANSPARENT_PNG).decode("ascii")

        # RAM koruması
        if len(r.content) > 6_000_000:
            return "data:image/png;base64," + base64.b64encode(_TRANSPARENT_PNG).decode("ascii")

        mime = _guess_mime(url, r.headers.get("content-type"))
        b64 = base64.b64encode(r.content).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return "data:image/png;base64," + base64.b64encode(_TRANSPARENT_PNG).decode("ascii")


# -------------------------
# Playwright (strong recovery)
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
    """Playwright/driver öldüyse komple reset."""
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
    """Browser yoksa/ölüyse ayağa kaldır. Driver öldüyse restart."""
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
            # "handler is closed" gibi durumlarda full restart şart
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
                # data-uri görseller anında gelir ama font/svg için kısa nefes payı:
                await page.wait_for_timeout(120)

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
            # Browser/driver öldüyse reset + 1 retry
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
    product_image_secondary_2: str = Query(""),
    logo_url: str = Query(""),
    theme: str = Query("classic"),
):
    price = format_currency_tr(price)
    sale_price = format_currency_tr(sale_price)
    title = tr_title_case(title)

    old_hidden, new_hidden, single_hidden = hidden_flags(price, sale_price)

    pct = calc_discount_percent(price, sale_price)
    discount_hidden = "hidden" if pct is None else ""
    discount_text = f"%{pct} İNDİRİM" if pct is not None else ""

    # ✅ BURASI MUTLAKA FONKSİYONUN İÇİNDE (4 boşluk içeride) OLACAK
    if theme == "season":
        template_path = os.path.join(BASE_DIR, "template_season.html")
        css_path = os.path.join(BASE_DIR, "styles_season.css")
    else:
        template_path = os.path.join(BASE_DIR, "template.html")
        css_path = os.path.join(BASE_DIR, "styles.css")

    with open(template_path, "r", encoding="utf-8") as f:
        tpl = f.read()
    with open(css_path, "r", encoding="utf-8") as f:
        css = f.read()

    if not logo_url:
        base_url = get_base_url(request)
        logo_url = f"{base_url}/static/vatkalilogo.svg"

    async with httpx.AsyncClient(follow_redirects=True) as client:
        product_image_primary, product_image_secondary_1, product_image_secondary_2, logo_url = await asyncio.gather(
            to_data_uri(product_image_primary, client),
            to_data_uri(product_image_secondary_1, client),
            to_data_uri(product_image_secondary_2, client),
            to_data_uri(logo_url, client),
        )

    html = tpl.replace("{{CSS}}", css)
    html = html.replace("{{product_image_primary}}", product_image_primary)
    html = html.replace("{{product_image_secondary_1}}", product_image_secondary_1)
    html = html.replace("{{product_image_secondary_2}}", product_image_secondary_2)
    html = html.replace("{{logo_url}}", logo_url)
    html = html.replace("{{title}}", title)

    # classic template değişkenleri:
    html = html.replace("{{price}}", price)
    html = html.replace("{{sale_price}}", sale_price)
    html = html.replace("{{old_hidden}}", old_hidden)
    html = html.replace("{{new_hidden}}", new_hidden)
    html = html.replace("{{single_hidden}}", single_hidden)
    html = html.replace("{{discount_text}}", discount_text)
    html = html.replace("{{discount_hidden}}", discount_hidden)

    try:
        png = await render_png(html, width=1080, height=1080)
    except Exception as e:
        print("RENDER_FAILED:", repr(e))
        png = _TRANSPARENT_PNG

    headers = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    return Response(content=png, media_type="image/png", headers=headers)


@app.get("/feed.xml", response_class=PlainTextResponse)
async def feed_proxy(request: Request):
    base_url = get_base_url(request)
    fv = (request.query_params.get("v") or "").strip()

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(FEED_URL)
        r.raise_for_status()

    root = ET.fromstring(r.text)
    channel = root.find("channel")
    if channel is None:
        return PlainTextResponse(r.text, media_type="application/xml")

    items = channel.findall("item")
    ns = {"g": "http://base.google.com/ns/1.0"}

    for item in items:
        title = tr_title_case((item.findtext("title") or "").strip())

        price = format_currency_tr(item.findtext("g:price", default="", namespaces=ns) or "")
        sale = format_currency_tr(item.findtext("g:sale_price", default="", namespaces=ns) or "")

        primary, s1, s2 = choose_images(item)

        # ✅ custom_label_1 -> theme (namespace bağımsız + unicode normalize)
        custom_label_1 = find_text_by_localname(item, "custom_label_1")
        theme = "season" if is_season_label(custom_label_1) else "classic"

        sig = build_sig(title, price, sale, primary, s1, s2, fv, theme)

        render_url = (
            f"{base_url}/render.png"
            f"?title={quote_plus(title)}"
            f"&price={quote_plus(price)}"
            f"&sale_price={quote_plus(sale)}"
            f"&product_image_primary={quote_plus(primary)}"
            f"&product_image_secondary_1={quote_plus(s1)}"
            f"&product_image_secondary_2={quote_plus(s2)}"
            f"&theme={quote_plus(theme)}"
            f"&fv={quote_plus(fv)}"
            f"&v={sig}"
        )

        img = item.find("g:image_link", ns)
        if img is None:
            img = ET.SubElement(item, "{http://base.google.com/ns/1.0}image_link")
        img.text = render_url

        # ek görsel linklerini 2 adet render_url ile set et
        for extra in item.findall("g:additional_image_link", ns):
            item.remove(extra)
        for _ in range(2):
            extra = ET.SubElement(item, "{http://base.google.com/ns/1.0}additional_image_link")
            extra.text = render_url

    xml_out = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    return PlainTextResponse(xml_out, media_type="application/xml", headers=headers)


@app.get("/probe")
async def probe(url: str = Query(...)):
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome Safari",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "Referer": "https://www.vatkali.com/",
        "Origin": "https://www.vatkali.com",
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
