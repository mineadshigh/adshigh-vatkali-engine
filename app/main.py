import base64
import hashlib
import mimetypes
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


# --- ✅ Görselleri indirip data-uri yap (Playwright'in dış görsel çekememe sorununu çözer) ---
def to_data_uri(url: str, timeout: int = 30) -> str:
    if not url:
        return ""

    # zaten data: ise dokunma
    if url.startswith("data:"):
        return url

    # svg ise text olarak embed
    if url.lower().split("?")[0].endswith(".svg"):
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        svg_text = r.text
        b64 = base64.b64encode(svg_text.encode("utf-8")).decode("ascii")
        return f"data:image/svg+xml;base64,{b64}"

    # normal görsel
    r = httpx.get(url, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    content = r.content
    mime = r.headers.get("content-type") or mimetypes.guess_type(url)[0] or "image/jpeg"
    b64 = base64.b64encode(content).decode("ascii")
    return f"data:{mime};base64,{b64}"


def wait_images(page):
    # tüm img'lerin load/err bitmesini bekle
    page.evaluate(
        """() => Promise.all(Array.from(document.images).map(img => {
            if (img.complete) return Promise.resolve();
            return new Promise(res => { img.onload = img.onerror = () => res(); });
        }))"""
    )


def render_png(html: str, width=1080, height=1350) -> bytes:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": width, "height": height, "deviceScaleFactor": 2})

        page.set_content(html, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=15000)
        wait_images(page)
        page.wait_for_timeout(200)

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

    base_dir = os.path.dirname(os.path.abspath(__file__))  # .../srv/app
    template_path = os.path.join(base_dir, "template.html")
    css_path = os.path.join(base_dir, "styles.css")

    with open(template_path, "r", encoding="utf-8") as f:
        tpl = f.read()
    with open(css_path, "r", encoding="utf-8") as f:
        css = f.read()

    # logo parametresi gelmezse otomatik ver
    if not logo_url:
        base_url = get_base_url(request)
        logo_url = f"{base_url}/static/vatkalilogo.svg"

    # ✅ burası senin anlamadığın kısım: HTML'i doldurmadan önce URL'leri data-uri yapıyoruz
    # Böylece Playwright dışarıdan resim çekmese bile render'da görünür.
    try:
        product_image_primary = to_data_uri(product_image_primary) if product_image_primary else ""
        product_image_secondary_1 = to_data_uri(product_image_secondary_1) if product_image_secondary_1 else ""
        product_image_secondary_2 = to_data_uri(product_image_secondary_2) if product_image_secondary_2 else ""
        logo_url = to_data_uri(logo_url) if logo_url else ""
    except Exception:
        # herhangi bir görsel indirilemezse boş geçsin (render yine çalışsın)
        pass

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

    r = httpx.get(FEED_URL, timeout=60, follow_redirects=True)
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
