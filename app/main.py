import os
import json
import uuid
import aiosqlite
import httpx
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
import stripe

# ── Database ──────────────────────────────────────────────────────────────

async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    db = await get_db()
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            stripe_session_id TEXT,
            status TEXT DEFAULT 'pending',
            customer_email TEXT,
            customer_name TEXT,
            product_type TEXT,
            width_inches REAL,
            height_inches REAL,
            quantity INTEGER,
            material TEXT,
            finish TEXT,
            artwork_filename TEXT,
            notes TEXT,
            unit_price_cents INTEGER,
            subtotal_cents INTEGER,
            shipping_cents INTEGER,
            total_cents INTEGER,
            paid INTEGER DEFAULT 0,
            fulfillment_status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.commit()
    await db.close()


app = FastAPI(title="Lucid Store — Custom Signs, Decals & Stickers")

BASE_DIR = Path(__file__).resolve().parent.parent
jinja_env = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=select_autoescape(["html"]),
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
LUCID_EMAIL = os.getenv("LUCID_EMAIL", "sales@lucidvinyl.com")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
DOMAIN = os.getenv("DOMAIN", "http://localhost:8000")
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "orders.db"))

stripe.api_key = STRIPE_SECRET_KEY


# ── Pricing engine ───────────────────────────────────────────────────────

MATERIALS = {
    # ── Vinyl ──
    "standard_vinyl":    {"name": "Standard Vinyl (3M 50-series)",   "base_cents_per_sqft": 800,  "category": "vinyl"},
    "premium_vinyl":     {"name": "Premium Vinyl (3M 1080/2080)",   "base_cents_per_sqft": 1200, "category": "vinyl"},
    "reflective":        {"name": "Reflective Vinyl",                "base_cents_per_sqft": 1500, "category": "vinyl"},
    "clear":             {"name": "Clear Vinyl",                     "base_cents_per_sqft": 900,  "category": "vinyl"},
    # ── Wood ──
    "plywood_half":      {"name": '½" Birch Plywood',               "base_cents_per_sqft": 2500, "category": "wood"},
    # ── Aluminum ──
    "aluminum_040":      {"name": 'Aluminum 0.040" (standard)',     "base_cents_per_sqft": 2000, "category": "metal"},
    "aluminum_060":      {"name": 'Aluminum 0.060" (heavy-duty)',   "base_cents_per_sqft": 2800, "category": "metal"},
    # ── Coroplast (sandwich board inserts) ──
    "coroplast":         {"name": "Coroplast (Corrugated Plastic)",  "base_cents_per_sqft": 500,  "category": "plastic"},
}

FINISHES = {
    "gloss":           {"name": "Gloss",                 "multiplier": 1.0},
    "matte":           {"name": "Matte",                 "multiplier": 1.0},
    "laminated_gloss": {"name": "Laminated Gloss",       "multiplier": 1.3},
    "laminated_matte": {"name": "Laminated Matte",       "multiplier": 1.3},
    "contour_cut":     {"name": "Contour Cut",           "multiplier": 1.5},
    "natural":         {"name": "Natural (unfinished)",  "multiplier": 1.0},
    "sealed":          {"name": "Clear-Sealed",          "multiplier": 1.15},
    "painted":         {"name": "Painted Finish",        "multiplier": 1.3},
}

PRODUCT_TYPES = {
    # ── Vinyl / Decals (sqft-based) ──
    "stickers":         {"name": "Custom Stickers",       "min_qty": 50, "setup_fee_cents": 1500, "pricing": "sqft",   "category": "decals"},
    "decals":           {"name": "Custom Decals",         "min_qty": 10, "setup_fee_cents": 2000, "pricing": "sqft",   "category": "decals"},
    "car_decals":       {"name": "Car Decals",            "min_qty": 1,  "setup_fee_cents": 2000, "pricing": "sqft",   "category": "decals"},
    "vehicle_graphics": {"name": "Vehicle Graphics",      "min_qty": 1,  "setup_fee_cents": 3500, "pricing": "sqft",   "category": "decals"},
    # ── Sandwich Boards (unit-priced) ──
    "sandwich_18x24":   {"name": 'Sandwich Board — 18×24"', "min_qty": 1, "base_price_cents": 13900, "pricing": "unit",  "category": "signage"},
    "sandwich_24x36":   {"name": 'Sandwich Board — 24×36"', "min_qty": 1, "base_price_cents": 18900, "pricing": "unit",  "category": "signage"},
    # ── Wood Signs (sqft-based) ──
    "wood_sign":        {"name": "Custom Wood Sign",       "min_qty": 1,  "setup_fee_cents": 2500, "pricing": "sqft",   "category": "signage"},
    # ── Aluminum Signs (sqft-based) ──
    "aluminum_sign":    {"name": "Custom Aluminum Sign",   "min_qty": 1,  "setup_fee_cents": 2500, "pricing": "sqft",   "category": "signage"},
}

# Default materials per category (for the UI to pre-select)
CATEGORY_DEFAULT_MATERIAL = {
    "decals":  "premium_vinyl",
    "signage": "plywood_half",
}

BASE_SHIPPING_CENTS = 1500  # $15 CAD flat rate


def calculate_price(product_type: str, width: float, height: float,
                    quantity: int, material: str, finish: str) -> dict:
    """Calculate price for a custom order. Two pricing models: unit (fixed per-item) and sqft (dimension-based)."""
    prod = PRODUCT_TYPES.get(product_type, PRODUCT_TYPES["stickers"])
    mat = MATERIALS.get(material, MATERIALS["standard_vinyl"])
    fin = FINISHES.get(finish, FINISHES["gloss"])
    margin = 1.30  # 30% margin

    # ── Unit pricing (sandwich boards, pre-sized products) ──
    if prod.get("pricing") == "unit":
        unit_price = prod["base_price_cents"]
        subtotal_cents = unit_price * quantity

        # Volume discount
        if quantity >= 10:
            subtotal_cents = int(subtotal_cents * 0.85)
        elif quantity >= 5:
            subtotal_cents = int(subtotal_cents * 0.90)
        elif quantity >= 3:
            subtotal_cents = int(subtotal_cents * 0.95)

        shipping_cents = BASE_SHIPPING_CENTS + (quantity - 1) * 800  # $15 + $8 each additional
        total_cents = subtotal_cents + shipping_cents

        return {
            "unit_price_cents": unit_price,
            "subtotal_cents": subtotal_cents,
            "shipping_cents": shipping_cents,
            "total_cents": total_cents,
            "pricing_model": "unit",
        }

    # ── Sqft-based pricing (decals, wood signs, aluminum signs) ──
    sqft = (width * height) / 144  # sq inches → sq ft
    total_sqft = sqft * quantity

    # Material cost
    material_cost_cents = int(total_sqft * mat["base_cents_per_sqft"] * fin["multiplier"])

    # Setup fee
    setup_fee_cents = prod.get("setup_fee_cents", 0)

    # Subtotal with margin
    base_subtotal = material_cost_cents + setup_fee_cents
    subtotal_cents = int(base_subtotal * margin)

    # Volume discount
    if quantity >= 1000:
        subtotal_cents = int(subtotal_cents * 0.80)
    elif quantity >= 500:
        subtotal_cents = int(subtotal_cents * 0.85)
    elif quantity >= 250:
        subtotal_cents = int(subtotal_cents * 0.90)
    elif quantity >= 100:
        subtotal_cents = int(subtotal_cents * 0.95)

    shipping_cents = BASE_SHIPPING_CENTS + (quantity - 1) * 500
    total_cents = subtotal_cents + shipping_cents

    return {
        "sqft_per_unit": round(sqft, 3),
        "total_sqft": round(total_sqft, 2),
        "material_cost_cents": material_cost_cents,
        "setup_fee_cents": setup_fee_cents,
        "unit_price_cents": round(subtotal_cents / quantity),
        "subtotal_cents": subtotal_cents,
        "shipping_cents": shipping_cents,
        "total_cents": total_cents,
        "pricing_model": "sqft",
    }


# ── Template helper ─────────────────────────────────────────────────────

def render_template(name: str, request: Request = None, **context) -> HTMLResponse:
    """Render a Jinja2 template with the given context."""
    ctx = dict(context)
    if request is not None:
        ctx["request"] = request
    template = jinja_env.get_template(name)
    return HTMLResponse(template.render(**ctx))


# ── Routes ────────────────────────────────────────────────────────────────

_db_initialized = False

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    global _db_initialized
    if not _db_initialized:
        await init_db()
        _db_initialized = True
    return render_template("index.html", request=request,
        materials=MATERIALS, finishes=FINISHES, product_types=PRODUCT_TYPES,
        category_defaults=CATEGORY_DEFAULT_MATERIAL)


@app.post("/api/quote")
async def get_quote(
    product_type: str = Form(...),
    width: float = Form(...),
    height: float = Form(...),
    quantity: int = Form(...),
    material: str = Form("standard_vinyl"),
    finish: str = Form("gloss"),
):
    """Return a live price quote without creating an order."""
    prod = PRODUCT_TYPES.get(product_type)
    if not prod:
        raise HTTPException(400, "Invalid product type")

    if quantity < prod["min_qty"]:
        return JSONResponse({
            "error": f"Minimum order is {prod['min_qty']} units for {prod['name']}",
            "min_qty": prod["min_qty"],
        }, status_code=400)

    if width <= 0 or height <= 0:
        prod = PRODUCT_TYPES.get(product_type, {})
        if prod.get("pricing") != "unit":
            raise HTTPException(400, "Dimensions must be positive")

    price = calculate_price(product_type, width, height, quantity, material, finish)
    return JSONResponse(price)


@app.post("/api/create-checkout")
async def create_checkout(
    request: Request,
    product_type: str = Form(...),
    width: float = Form(...),
    height: float = Form(...),
    quantity: int = Form(...),
    material: str = Form("standard_vinyl"),
    finish: str = Form("gloss"),
    customer_email: str = Form(...),
    customer_name: str = Form(""),
    notes: str = Form(""),
):
    """Create a Stripe Checkout session and save the order."""
    prod = PRODUCT_TYPES.get(product_type)
    if not prod:
        raise HTTPException(400, "Invalid product type")
    if quantity < prod["min_qty"]:
        raise HTTPException(400, f"Minimum {prod['min_qty']} units required")

    if prod.get("pricing") != "unit" and (width <= 0 or height <= 0):
        raise HTTPException(400, "Dimensions must be positive")

    price = calculate_price(product_type, width, height, quantity, material, finish)
    order_id = str(uuid.uuid4())[:8]

    # Save order to DB
    db = await get_db()
    await db.execute("""
        INSERT INTO orders (id, customer_email, customer_name, product_type,
            width_inches, height_inches, quantity, material, finish, notes,
            unit_price_cents, subtotal_cents, shipping_cents, total_cents, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'checkout')
    """, (order_id, customer_email, customer_name, product_type,
          width, height, quantity, material, finish, notes,
          price["unit_price_cents"], price["subtotal_cents"],
          price["shipping_cents"], price["total_cents"]))
    await db.commit()

    # Create Stripe session
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "cad",
                "product_data": {
                    "name": f"{prod['name']} — {quantity} units",
                    "description": f'{width}"×{height}", {MATERIALS[material]["name"]}, {FINISHES[finish]["name"]}',
                },
                "unit_amount": price["total_cents"],
            },
            "quantity": 1,
        }],
        mode="payment",
        customer_email=customer_email,
        metadata={"order_id": order_id},
        success_url=f"{DOMAIN}/order/{order_id}/success",
        cancel_url=f"{DOMAIN}/order/{order_id}/cancelled",
    )

    # Save stripe session ID
    await db.execute(
        "UPDATE orders SET stripe_session_id = ? WHERE id = ?",
        (session.id, order_id)
    )
    await db.commit()
    await db.close()

    return RedirectResponse(session.url, status_code=303)


@app.get("/order/{order_id}/success", response_class=HTMLResponse)
async def order_success(request: Request, order_id: str):
    db = await get_db()
    order = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    row = await order.fetchone()
    await db.close()

    if not row:
        raise HTTPException(404, "Order not found")

    return render_template("success.html", request=request, order=dict(row))


@app.get("/order/{order_id}/cancelled", response_class=HTMLResponse)
async def order_cancelled(request: Request, order_id: str):
    return render_template("cancelled.html", request=request)


@app.post("/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(400, "Invalid signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = session.get("metadata", {}).get("order_id")
        if order_id:
            db = await get_db()

            # Mark as paid
            await db.execute(
                "UPDATE orders SET status = 'paid', paid = 1, stripe_session_id = ? WHERE id = ?",
                (session.id, order_id)
            )
            await db.commit()

            # Get order details for email
            order = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
            row = await order.fetchone()
            await db.close()

            if row:
                await send_fulfillment_email(dict(row))

    return JSONResponse({"status": "ok"})


# ── Email notification ───────────────────────────────────────────────────

async def send_fulfillment_email(order: dict):
    """Send order details to Lucid Vinyl for fulfillment."""
    prod = PRODUCT_TYPES.get(order["product_type"], {})
    mat = MATERIALS.get(order["material"], {})
    fin = FINISHES.get(order["finish"], {})

    subject = f"New Order #{order['id']} — {prod.get('name', 'Custom')} ({order['quantity']} units)"
    body = f"""
NEW ORDER — Lucid Vinyl Store

Order #: {order['id']}
Date: {order.get('created_at', 'N/A')}
Status: PAID

CUSTOMER:
  Name: {order['customer_name'] or 'N/A'}
  Email: {order['customer_email']}

ORDER DETAILS:
  Product: {prod.get('name', order['product_type'])}
  Size: {order['width_inches']}" × {order['height_inches']}"
  Quantity: {order['quantity']}
  Material: {mat.get('name', order['material'])}
  Finish: {fin.get('name', order['finish'])}
  Notes: {order.get('notes') or 'None'}

FINANCIAL:
  Unit Price: ${order['unit_price_cents'] / 100:.2f}
  Subtotal: ${order['subtotal_cents'] / 100:.2f}
  Total: ${order['total_cents'] / 100:.2f}

Please fulfill this order and notify the customer when shipped.
"""
    if SENDGRID_API_KEY:
        async with httpx.AsyncClient() as client:
            await client.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={
                    "Authorization": f"Bearer {SENDGRID_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "personalizations": [{"to": [{"email": LUCID_EMAIL}]}],
                    "from": {"email": "orders@lucidvinyl.com", "name": "Lucid Vinyl Store"},
                    "subject": subject,
                    "content": [{"type": "text/plain", "value": body}],
                },
            )


# ── Static file serving for uploaded artwork ─────────────────────────────

uploads_dir = BASE_DIR / "static" / "uploads"
uploads_dir.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
