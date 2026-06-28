import os
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from pydantic import BaseModel
from playwright.async_api import async_playwright
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

class CORSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin", "*")

        # Preflight isteği
        if request.method == "OPTIONS":
            response = JSONResponse(content={}, status_code=200)
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept, Origin, X-Requested-With"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Max-Age"] = "600"
            return response

        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept, Origin, X-Requested-With"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

app.add_middleware(CORSMiddleware)

# Store credentials from .env
STORES = {}
for i in range(1, 7):
    name = os.getenv(f"STORE_{i}_NAME")
    if name:
        STORES[name] = {
            "username": os.getenv(f"STORE_{i}_USERNAME"),
            "password": os.getenv(f"STORE_{i}_PASSWORD"),
        }

TAB_URLS = {
    "new": "https://business.umico.az/account/orders?state=new",
    "on_package": "https://business.umico.az/account/orders?state=on_package",
    "pickup": "https://business.umico.az/account/orders?state=pickup",
}


class ScrapeRequest(BaseModel):
    store_name: str
    tab: str  # new | on_package | pickup


async def login(page, username, password):
    await page.goto("https://business.umico.az/sign-in", wait_until="networkidle")
    await page.fill('[data-testid="username"]', username)
    await page.fill('[data-testid="password"]', password)
    await page.keyboard.press("Enter")
    await page.wait_for_load_state("networkidle")
    try:
        popup = page.locator('[data-testid="quality-control-overlay-button"]')
        if await popup.is_visible(timeout=5000):
            await popup.click()
            await page.wait_for_timeout(1000)
    except:
        pass


async def get_tab_counts(page):
    counts = {"new": 0, "on_package": 0, "pickup": 0}
    try:
        new_el = page.locator('#order-submenu-state-new .v-list-item-title span')
        on_el = page.locator('#order-submenu-state-on_package .v-list-item-title span')
        pickup_el = page.locator('#order-submenu-state-pickup .v-list-item-title span')

        def extract_count(text):
            import re
            m = re.search(r'\((\d+)\)', text or "")
            return int(m.group(1)) if m else 0

        counts["new"] = extract_count(await new_el.text_content())
        counts["on_package"] = extract_count(await on_el.text_content())
        counts["pickup"] = extract_count(await pickup_el.text_content())
    except:
        pass
    return counts


async def scroll_and_collect_rows(page):
    table = page.locator('[data-testid="orders-table"]')
    prev_count = 0
    while True:
        rows = table.locator('[data-testid="order-row"]')
        count = await rows.count()
        if count == prev_count:
            break
        prev_count = count
        await page.evaluate("""
            const el = document.querySelector('[data-testid="orders-table"]');
            if (el) el.scrollTop = el.scrollHeight;
        """)
        await page.wait_for_timeout(1200)
    return rows


async def scrape_order_detail(page):
    await page.wait_for_timeout(800)
    order = {}

    try:
        order["order_number"] = (await page.locator('[data-testid="order-detail-number"]').text_content() or "").strip()
    except:
        order["order_number"] = ""

    try:
        order["status"] = (await page.locator('[data-testid="order-status-badge"]').text_content() or "").strip()
    except:
        order["status"] = ""

    try:
        hints = page.locator('.tw\\:text-umico-text-hint')
        count = await hints.count()
        order["courier_arrival"] = ""
        for i in range(count):
            text = await hints.nth(i).text_content()
            if "Kuryerin gəlişi" in (text or ""):
                sibling = hints.nth(i).locator('xpath=../div[2]')
                order["courier_arrival"] = (await sibling.text_content() or "").strip()
                break
    except:
        order["courier_arrival"] = ""

    boxes = []
    try:
        box_els = page.locator('.tw\\:ring-1.tw\\:ring-umico-stroke-strokes-second')
        box_count = await box_els.count()
        for i in range(box_count):
            box = box_els.nth(i)
            box_data = {}

            try:
                box_data["box_title"] = (await box.locator('.tw\\:text-lg').text_content() or "").strip()
            except:
                box_data["box_title"] = f"Qutu #{i+1}"

            try:
                logistic_els = box.locator('.tw\\:text-\\[15px\\].tw\\:leading-\\[23px\\].tw\\:flex')
                box_data["logistics_number"] = (await logistic_els.text_content() or "").replace("Loqistika nömrəsi:", "").strip()
            except:
                box_data["logistics_number"] = ""

            try:
                img = box.locator('img').first
                box_data["image"] = await img.get_attribute("src") or ""
            except:
                box_data["image"] = ""

            try:
                box_data["product_name"] = (await box.locator('p').first.text_content() or "").strip()
            except:
                box_data["product_name"] = ""

            try:
                hint_divs = box.locator('.tw\\:text-umico-text-hint')
                h_count = await hint_divs.count()
                for j in range(h_count):
                    label = (await hint_divs.nth(j).text_content() or "").strip()
                    value_el = hint_divs.nth(j).locator('xpath=../div[2]')
                    value = (await value_el.text_content() or "").strip()
                    if label == "Miqdar":
                        box_data["quantity"] = value
                    elif label == "Qiymət":
                        box_data["price"] = value
                    elif label == "Barkod":
                        box_data["barcode"] = value
                    elif label == "Artikul":
                        box_data["artikul"] = value
            except:
                pass

            boxes.append(box_data)
    except:
        pass

    order["boxes"] = boxes
    return order


@app.get("/stores")
def get_stores():
    return {"stores": list(STORES.keys())}


@app.post("/scrape/counts")
async def scrape_counts(body: dict):
    store_name = body.get("store_name")
    if store_name not in STORES:
        raise HTTPException(status_code=404, detail="Mağaza tapılmadı")

    creds = STORES[store_name]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await login(page, creds["username"], creds["password"])
            await page.goto("https://business.umico.az/account/orders?state=new", wait_until="networkidle")
            await page.wait_for_timeout(2000)
            counts = await get_tab_counts(page)
            return {"counts": counts}
        finally:
            await browser.close()


@app.post("/scrape/orders")
async def scrape_orders(req: ScrapeRequest):
    if req.store_name not in STORES:
        raise HTTPException(status_code=404, detail="Mağaza tapılmadı")
    if req.tab not in TAB_URLS:
        raise HTTPException(status_code=400, detail="Yanlış tab")

    creds = STORES[req.store_name]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await login(page, creds["username"], creds["password"])
            await page.goto(TAB_URLS[req.tab], wait_until="networkidle")
            await page.wait_for_timeout(2000)

            counts = await get_tab_counts(page)
            rows = await scroll_and_collect_rows(page)
            row_count = await rows.count()

            orders = []
            for i in range(row_count):
                try:
                    await rows.nth(i).click()
                    await page.wait_for_timeout(1000)
                    detail = await scrape_order_detail(page)
                    orders.append(detail)
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(500)
                except Exception as e:
                    orders.append({"error": str(e), "index": i})

            return {
                "store": req.store_name,
                "tab": req.tab,
                "counts": counts,
                "orders": orders
            }
        finally:
            await browser.close()


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok"}