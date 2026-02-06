import base64
import hashlib
import os
from urllib.parse import (
    quote_plus,
    urlsplit,
    urlunsplit,
    parse_qsl,
    urlencode,
)
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

NS = {"g": "http://base.google.com/ns/1.0"}

# ---- Helpers ----

def norm_price(s: str) -> str:
    return " ".join((s or "").split()).strip()


def format_currency_tr(s: str) -> str:
    """Feed bazen 'TRY' gönderiyor. Görselde 'TL' gösterelim."""
    x = norm_price(s)
    if not x:
        return x
    return x.replace("TRY", "TL").replace("try", "TL")


def _clean_url(u: str) -> str:
    """fbclid/utm gibi takip parametrelerini temizle, duplicate görselleri yakalayalım."""
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
    """
    primary + 2 secondary seçer.
    - fbclid/utm temizleyerek duplicate yakalar
    - sırayı koruyarak unique listeden seçer
    """
    primary_raw = (item.findtext("g:image_link", default="", namespaces=NS) or "").strip()
    # Bazı feedlerde namespaceless olabiliyor, onu da yakala:
    if not primary_raw:
        primary_raw = (item.findtext("image_link", default="") or "").strip()

    additional_raw = [
        (e.text or "").strip()
        for e in item.findall("g:additional_image_link", namespaces=NS)
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

    # fallback (render tarafında boş gelmesin)
    if not s1:
        s1 = primary
    if not s2:
        s2 = s1

    return primary, s1, s2


def hidden_flags(price: str, sale: str):
    p = norm_price(price)
    s = norm_price(sale)
    if (not s) or (s == p):
        return ("hidden", "hidden", "")  # old_hidden, new_hidden, single_hidden
    return ("", "", "hidden")


def build_sig(*parts: str) -> str:
    raw = "|".join([p or "" for p in parts]).encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:12]


def get_base_url(request: Request) -> str:
    return APP_BASE_URL if APP_BASE_URL else str(request.base_url).rstrip("/")


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


_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome Safari",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": "https://www.vatkali.com/",
    "Origin": "https://www.vatkali.com",
}


def to_data_uri(url: str) -> str:
    """
    Ürün görselini base64 data-uri yapıyoruz.
    Bazı ürünlerde boş preview gelmesinin ana sebebi: görsel URL'i 200 dönmüyor / HTML dönüyor / timeout.
    O yüzden: retry + HTML ise transparan fallback.
    """
    if not url:
        return "data:image/png;base64," + base64.b64encode(_TRANSPARENT_PNG).decode("ascii")

    if url.startswith("data:"):
        return url

    for _ in range(2):  # retry
        try:
            with httpx.Client(follow_redirects=True, timeout=35, headers=_DEFAULT_HEADERS) as client:
                r = client.get(url)
                r.raise_for_status()

                ct = (r.headers.get("content-type") or "").lower()
                # Bazı CDN'ler bot'a HTML döndürüyor, bunu görsel diye basınca boş kalıyor:
                if "text/html" in ct or "application/json" in ct:
                    break

                mime = _guess_mime(url, r.headers.get("content-type"))
                b64 = base64.b64encode(r.content).decode("ascii")
                return f"data:{mime};base64,{b64}"
        except Exception:
            continue

    return "data:image/png;base64," + base64.b64encode(_TRANSPARENT_PNG).decode("ascii")


def render_png(html: str, width=1080, height=1080) -> bytes:
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": width, "height": height, "deviceScaleFactor": 2})

            page.set_content(html, wait_until="domcontentloaded")
            page.wait_for_timeout(350)  # biraz daha güvenli

            frame = page.locator(".frame")
            frame.wait_for(state="visible", timeout=7000)

            return frame.screenshot(type="png")
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass


# ---- Endpoints ----

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
    price = format_currency_tr(price)
    sale_price = format_currency_tr(sale_price)

    old_hidden, new_hidden, single_hidden = hidden_flags(price, sale_price)

    template_path = os.path.join(BASE_DIR, "template.html")
    css_path = os.path.join(BASE_DIR, "styles.css")

    with open(template_path, "r", encoding="utf-8") as f:
        tpl = f.read()
    with open(css_path, "r", encoding="utf-8") as f:
        css = f.read()

    if not logo_url:
        base_url = get_base_url(request)
        logo_url = f"{base_url}/static/vatkalilogo.svg"

    product_image_primary = to_data_uri(product_image_primary)
    product_image_secondary_1 = to_data_uri(product_image_secondary_1)
    product_image_secondary_2 = to_data_uri(product_image_secondary_2)
    logo_url = to_data_uri(logo_url)

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
def feed_proxy(request: Request):
    """
    ✅ LIMIT YOK: tüm ürünlere frame basar.
    ✅ image_link + additional_image_link tamamen frame olur.
    """
    base_url = get_base_url(request)

    r = httpx.get(FEED_URL, timeout=60)
    r.raise_for_status()

    root = ET.fromstring(r.text)
    channel = root.find("channel")
    if channel is None:
        return PlainTextResponse(r.text, media_type="application/xml")

    items = channel.findall("item")  # ✅ TAMAMI

    for item in items:
        title = (item.findtext("title") or "").strip()
        price = format_currency_tr(item.findtext("g:price", default="", namespaces=NS) or "")
        sale = format_currency_tr(item.findtext("g:sale_price", default="", namespaces=NS) or "")

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

        # --- image_link'i frame yap (namespace + namespaceless güvence) ---
        img = item.find("g:image_link", namespaces=NS)
        if img is None:
            img = item.find("image_link")  # bazı feedlerde bu var
        if img is None:
            img = ET.SubElement(item, "{http://base.google.com/ns/1.0}image_link")
        img.text = render_url

        # --- TÜM additional_image_link'leri SİL ---
        for extra in list(item.findall("g:additional_image_link", namespaces=NS)):
            item.remove(extra)

        # --- 2 tane frame additional ekle (Meta bypass etmesin + carousel stabil) ---
        for _ in range(2):
            extra = ET.SubElement(item, "{http://base.google.com/ns/1.0}additional_image_link")
            extra.text = render_url

    xml_out = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    return PlainTextResponse(xml_out, media_type="application/xml")


@app.get("/probe")
def probe(url: str = Query(...)):
    try:
        with httpx.Client(follow_redirects=True, timeout=25, headers=_DEFAULT_HEADERS) as client:
            r = client.get(url)
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
