import base64
import hashlib
import os
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import httpx
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright

# ENV
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
FEED_URL = os.getenv("FEED_URL", "https://www.vatkali.com/Xml/?Type=FACEBOOK&fname=vatkali")

app = FastAPI()

# Static: repo root'taki /frameassets -> /static
# repo: frameassets/vatkalilogo.svg
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # .../srv/app
STATIC_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frameassets"))  # .../srv/frameassets
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def norm_price(s: str) -> str:
    return " ".join((s or "").split()).strip()


def choose_images(item: ET.Element):
    ns = {"g": "http://base.google.com/ns/1.0"}
    primary = (item.findtext("g:image_link", default="", namespaces=ns) or "").strip()

    additional = [
        (e.text or "").strip()
        for e in item.findall("g:additional_image_link", namespaces=ns)
        if e is not None and (e.text or "").strip()
    ]

    # 1 büyük + 2 küçük
    s1 = additional[0] if len(additional) >= 1 else primary
    s2 = additional[1] if len(additional) >= 2 else (additional[0] if len(additional) >= 1 else primary)

    return primary, s1, s2


def hidden_flags(price: str, sale: str):
    p = norm_price(price)
    s = norm_price(sale)
    # sale yoksa veya aynıysa: sadece tek fiyat göster
    if (not s) or (s == p):
        return ("hidden", "hidden", "")  # old_hidden, new_hidden, single_hidden
    # sale varsa: old + new göster, single gizle
    return ("", "", "hidden")


def build_sig(*parts: str) -> str:
    raw = "|".join([p or "" for p in parts]).encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:12]


def get_base_url(request: Request) -> str:
    return APP_BASE_URL if APP_BASE_URL else str(request.base_url).rstrip("/")


# 1x1 transparent png (fallback)
_TRANSPARENT_PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("utf-8")


def to_data_uri(url: str) -> str:
    """
    Dış görseller bazen headless render sırasında gelmiyor.
    Bu fonksiyon görseli backend'te indirir ve data URI yapar (garanti çözüm).
    """
    url = (url or "").strip()
    if not url:
        return f"data:image/png;base64,{_TRANSPARENT_PNG}"

    # SVG ise direkt URL kalsın (istersen onu da indirebiliriz ama şimdilik gerek yok)
    if url.lower().endswith(".svg"):
        return url

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        # bazı CDN'ler hotlink kontrolü yapabiliyor → referer eklemek faydalı
        "Referer": "https://www.vatkali.com/",
    }

    try:
        with httpx.Client(follow_redirects=True, timeout=20.0, headers=headers) as client:
            r = client.get(url)
            r.raise_for_status()

            content_type = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            # content-type gelmezse jpg varsay
            if not content_type or not content_type.startswith("image/"):
                content_type = "image/jpeg"

            b64 = base64.b64encode(r.content).decode("utf-8")
            return f"data:{content_type};base64,{b64}"
    except Exception:
        return f"data:image/png;base64,{_TRANSPARENT_PNG}"


def render_png(html: str, width=1080, height=1350) -> bytes:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": width, "height": height, "deviceScaleFactor": 2})

        # data-uri kullandığımız için artık dış görsel bekleme derdi kalmıyor
        page.set_content(html, wait_until="load")
        page.wait_for_timeout(150)  # font/layout için mini buffer

        buf = page.screenshot(type="png", full_page=False)
        browser.close()
        return buf


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
    # hidden flag'leri backend hesaplıyor
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

    # ✅ kritik: dış görselleri data-uri'ye çevir (garanti yüklenir)
    product_image_primary = to_data_uri(product_image_primary)
    product_image_secondary_1 = to_data_uri(product_image_secondary_1)
    product_image_secondary_2 = to_data_uri(product_image_secondary_2)
    # logo svg olduğundan url kalabilir; istersen svg'yi de data-uri yaparız

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

    png = render_png(html, width=1080, height=1350)
    return Response(content=png, media_type="image/png")


@app.get("/feed.xml", response_class=PlainTextResponse)
def feed_proxy(request: Request, limit: int = 10):
    base_url = get_base_url(request)

    r = httpx.get(FEED_URL, timeout=60)
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

        img = item.find("g:image_link", ns)
        if img is None:
            img = ET.SubElement(item, "{http://base.google.com/ns/1.0}image_link")
        img.text = render_url

    xml_out = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    return PlainTextResponse(xml_out, media_type="application/xml")
