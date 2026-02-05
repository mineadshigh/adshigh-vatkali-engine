import hashlib
import os
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import httpx
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright

# Railway'de env'den gelecek; yoksa request üzerinden üretilecek
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
FEED_URL = os.getenv("FEED_URL", "https://www.vatkali.com/Xml/?Type=FACEBOOK&fname=vatkali")

app = FastAPI()

# -----------------------------
# Static: /frameassets -> /static
# repo yapın:
#   /app/main.py
#   /frameassets/vatkalilogo.svg
# -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # .../app
STATIC_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frameassets"))  # .../frameassets
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def norm_price(s: str) -> str:
    if not s:
        return ""
    return " ".join(s.split()).strip()


def choose_images(item: ET.Element):
    ns = {"g": "http://base.google.com/ns/1.0"}

    primary = (item.findtext("g:image_link", default="", namespaces=ns) or "").strip()

    additional = [
        e.text.strip()
        for e in item.findall("g:additional_image_link", namespaces=ns)
        if e is not None and e.text and e.text.strip()
    ]

    # layout: 1 büyük + 2 küçük
    s1 = additional[0] if len(additional) >= 1 else primary
    s2 = additional[1] if len(additional) >= 2 else (additional[0] if len(additional) >= 1 else primary)

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


def render_png(html: str, width=1080, height=1350) -> bytes:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": width, "height": height, "deviceScaleFactor": 2})
        page.set_content(html, wait_until="networkidle")
        buf = page.screenshot(type="png", full_page=False)
        browser.close()
        return buf


def get_base_url(request: Request) -> str:
    if APP_BASE_URL:
        return APP_BASE_URL
    return str(request.base_url).rstrip("/")


@app.get("/render.png")
def render_endpoint(
    title: str = Query(""),
    price: str = Query(""),
    sale_price: str = Query(""),
    product_image_primary: str = Query(""),
    product_image_secondary_1: str = Query(""),
    product_image_secondary_2: str = Query(""),
    logo_url: str = Query(""),
    old_hidden: str = Query(""),
    new_hidden: str = Query(""),
    single_hidden: str = Query(""),
):
    # app/template.html + app/styles.css
    template_path = os.path.join(BASE_DIR, "template.html")
    css_path = os.path.join(BASE_DIR, "styles.css")

    with open(template_path, "r", encoding="utf-8") as f:
        tpl = f.read()
    with open(css_path, "r", encoding="utf-8") as f:
        css = f.read()

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
    """
    Test için: feed'i çekip ilk N ürüne 'image_link' olarak bizim render URL'imizi basar.
    """
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
        old_hidden, new_hidden, single_hidden = hidden_flags(price, sale)

        # logo artık gerçekten servis ediliyor:
        logo_url = f"{base_url}/static/vatkalilogo.svg"

        sig = build_sig(title, price, sale, primary, s1, s2)

        render_url = (
            f"{base_url}/render.png"
            f"?title={quote_plus(title)}"
            f"&price={quote_plus(price)}"
            f"&sale_price={quote_plus(sale)}"
            f"&product_image_primary={quote_plus(primary)}"
            f"&product_image_secondary_1={quote_plus(s1)}"
            f"&product_image_secondary_2={quote_plus(s2)}"
            f"&logo_url={quote_plus(logo_url)}"
            f"&old_hidden={quote_plus(old_hidden)}"
            f"&new_hidden={quote_plus(new_hidden)}"
            f"&single_hidden={quote_plus(single_hidden)}"
            f"&v={sig}"
        )

        img = item.find("g:image_link", ns)
        if img is None:
            img = ET.SubElement(item, "{http://base.google.com/ns/1.0}image_link")
        img.text = render_url

    xml_out = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    return PlainTextResponse(xml_out, media_type="application/xml")
