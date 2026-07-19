import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

# ------------------------------------------------------------------
# الإعدادات (تُقرأ من متغيرات البيئة - لا تكتب أي شيء سري هنا)
# ------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
CRYPTO_PAY_TOKEN = os.environ.get("CRYPTO_PAY_TOKEN", "")
BINANCE_PAY_ID = os.environ.get("BINANCE_PAY_ID", "غير محدد بعد")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")  # رابط الموقع بعد النشر (https)
PORT = int(os.environ.get("PORT", 8080))
DB_PATH = os.environ.get("DB_PATH", "store.db")
CRYPTO_API_BASE = "https://pay.crypt.bot/api"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("store_bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ------------------------------------------------------------------
# قاعدة البيانات (SQLite - ملف واحد بسيط)
# ------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            price REAL NOT NULL,
            category TEXT DEFAULT 'عام',
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            sold INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            stock_id INTEGER,
            amount REAL,
            method TEXT,
            status TEXT DEFAULT 'pending',
            invoice_id TEXT,
            created_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------------
# أدوات مساعدة لقاعدة البيانات
# ------------------------------------------------------------------
def list_products():
    conn = db()
    rows = conn.execute(
        """SELECT p.*, (SELECT COUNT(*) FROM stock s WHERE s.product_id = p.id AND s.sold = 0) AS available
           FROM products p WHERE p.active = 1 ORDER BY p.id DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_product(pid):
    conn = db()
    row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def reserve_stock(product_id):
    """يمسك أول وحدة مخزون متاحة (بدون تسليمها بعد) ويرجع الصف"""
    conn = db()
    row = conn.execute(
        "SELECT * FROM stock WHERE product_id=? AND sold=0 LIMIT 1", (product_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_stock_sold(stock_id):
    conn = db()
    conn.execute("UPDATE stock SET sold=1 WHERE id=?", (stock_id,))
    conn.commit()
    conn.close()


def create_order(user_id, product_id, stock_id, amount, method, invoice_id=None, status="pending"):
    conn = db()
    cur = conn.execute(
        "INSERT INTO orders (user_id, product_id, stock_id, amount, method, status, invoice_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (user_id, product_id, stock_id, amount, method, status, invoice_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    oid = cur.lastrowid
    conn.close()
    return oid


def get_order(order_id=None, invoice_id=None):
    conn = db()
    if order_id:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM orders WHERE invoice_id=?", (invoice_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_order_status(order_id, status):
    conn = db()
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()
    conn.close()


async def deliver_order(order_id):
    """يرسل المحتوى (الحساب/الكود) للعميل ويقفل الطلب"""
    order = get_order(order_id=order_id)
    if not order or order["status"] == "delivered":
        return
    product = get_product(order["product_id"])
    stock_id = order["stock_id"]
    if stock_id is None:
        s = reserve_stock(order["product_id"])
        if not s:
            await bot.send_message(order["user_id"], "⚠️ عذراً، نفذت الكمية للمنتج. تواصل مع الدعم لاسترجاع المبلغ.")
            await bot.send_message(ADMIN_ID, f"⚠️ نفذ مخزون المنتج #{order['product_id']} لكن تم دفع طلب #{order_id}")
            return
        stock_id = s["id"]
    conn = db()
    stock_row = conn.execute("SELECT * FROM stock WHERE id=?", (stock_id,)).fetchone()
    conn.close()
    mark_stock_sold(stock_id)
    set_order_status(order_id, "delivered")
    await bot.send_message(
        order["user_id"],
        f"✅ تم الدفع بنجاح!\n\n<b>{product['name']}</b>\n\n📦 محتوى طلبك:\n<code>{stock_row['content']}</code>\n\nشكراً لتسوقك معنا 🌟",
    )
    await bot.send_message(ADMIN_ID, f"💰 طلب جديد مكتمل #{order_id} — {product['name']} — {order['amount']}$")


# ------------------------------------------------------------------
# التحقق من بيانات تليكرام Mini App (initData) — حماية من التلاعب
# ------------------------------------------------------------------
def validate_init_data(init_data: str):
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if computed_hash != received_hash:
            return None
        user = json.loads(parsed.get("user", "{}"))
        return user
    except Exception as e:
        log.warning("initData invalid: %s", e)
        return None


# ------------------------------------------------------------------
# CryptoBot API
# ------------------------------------------------------------------
async def cryptobot_create_invoice(amount: float, description: str):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{CRYPTO_API_BASE}/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN},
            json={
                "amount": str(amount),
                "currency_type": "fiat",
                "fiat": "USD",
                "accepted_assets": "USDT,TON,BTC",
                "description": description[:1024],
            },
        ) as resp:
            data = await resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"CryptoBot error: {data}")
            return data["result"]


def verify_cryptobot_signature(body_bytes: bytes, signature: str) -> bool:
    secret = hashlib.sha256(CRYPTO_PAY_TOKEN.encode()).digest()
    computed = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature or "")


# ------------------------------------------------------------------
# أزرار عامة
# ------------------------------------------------------------------
def main_menu_kb():
    kb = [[InlineKeyboardButton(text="🛍️ فتح المتجر", web_app=WebAppInfo(url=WEBAPP_URL))]] if WEBAPP_URL else []
    kb.append([InlineKeyboardButton(text="📋 عرض المنتجات هنا", callback_data="list_products")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def payment_kb(product_id):
    kb = [
        [InlineKeyboardButton(text="💎 الدفع عبر CryptoBot", callback_data=f"pay_crypto:{product_id}")],
        [InlineKeyboardButton(text="🟡 الدفع عبر Binance Pay", callback_data=f"pay_binance:{product_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ------------------------------------------------------------------
# أوامر المستخدم العادي
# ------------------------------------------------------------------
@router.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "👋 أهلاً بك في المتجر!\n\nتقدر تتصفح المنتجات وتشتري حسابات وأكواد سوفتوير بالدفع عبر:\n"
        "💎 CryptoBot (تلقائي وفوري)\n🟡 Binance Pay\n\nاضغط الزر تحت:",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "list_products")
async def list_products_cb(callback: CallbackQuery):
    products = list_products()
    if not products:
        await callback.message.answer("لا توجد منتجات حالياً.")
        await callback.answer()
        return
    for p in products:
        text = f"<b>{p['name']}</b>\n{p['description'] or ''}\n\n💵 السعر: {p['price']}$\n📦 المتوفر: {p['available']}"
        kb = payment_kb(p["id"]) if p["available"] > 0 else None
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("pay_crypto:"))
async def pay_crypto_cb(callback: CallbackQuery):
    if not CRYPTO_PAY_TOKEN:
        await callback.answer("لم يتم تفعيل CryptoBot بعد من الأدمن.", show_alert=True)
        return
    product_id = int(callback.data.split(":")[1])
    product = get_product(product_id)
    if not product:
        await callback.answer("المنتج غير موجود", show_alert=True)
        return
    try:
        invoice = await cryptobot_create_invoice(product["price"], product["name"])
    except Exception as e:
        log.exception("cryptobot error")
        await callback.answer("خطأ في إنشاء فاتورة الدفع، حاول لاحقاً.", show_alert=True)
        return
    order_id = create_order(
        callback.from_user.id, product_id, None, product["price"], "cryptobot", invoice_id=str(invoice["invoice_id"])
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💳 ادفع الآن", url=invoice["bot_invoice_url"])]]
    )
    await callback.message.answer(
        f"🧾 تم إنشاء فاتورة الدفع لطلب #{order_id}\nاضغط الزر تحت لإتمام الدفع، وبمجرد الدفع راح يوصلك المنتج تلقائياً.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_binance:"))
async def pay_binance_cb(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    product = get_product(product_id)
    if not product:
        await callback.answer("المنتج غير موجود", show_alert=True)
        return
    order_id = create_order(callback.from_user.id, product_id, None, product["price"], "binance", status="awaiting_payment")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ لقد دفعت", callback_data=f"binance_paid:{order_id}")]]
    )
    await callback.message.answer(
        f"🟡 الدفع عبر Binance Pay\n\nحوّل مبلغ <b>{product['price']}$</b> إلى:\n<code>{BINANCE_PAY_ID}</code>\n\n"
        f"بعد التحويل اضغط زر (لقد دفعت) تحت، وسيتم مراجعة الطلب #{order_id} وتسليمك المنتج خلال دقائق.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("binance_paid:"))
async def binance_paid_cb(callback: CallbackQuery):
    order_id = int(callback.data.split(":")[1])
    order = get_order(order_id=order_id)
    if not order:
        await callback.answer("الطلب غير موجود", show_alert=True)
        return
    set_order_status(order_id, "pending_review")
    product = get_product(order["product_id"])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تأكيد الدفع وتسليم", callback_data=f"admin_confirm:{order_id}"),
                InlineKeyboardButton(text="❌ رفض", callback_data=f"admin_reject:{order_id}"),
            ]
        ]
    )
    await bot.send_message(
        ADMIN_ID,
        f"🔔 طلب دفع Binance Pay جديد #{order_id}\nالمنتج: {product['name']}\nالمبلغ: {order['amount']}$\n"
        f"العميل: <a href='tg://user?id={order['user_id']}'>{order['user_id']}</a>",
        reply_markup=kb,
    )
    await callback.message.answer("⏳ تم إرسال طلبك للمراجعة، بيوصلك المنتج بمجرد التأكيد.")
    await callback.answer()


# ------------------------------------------------------------------
# أوامر الأدمن فقط
# ------------------------------------------------------------------
@router.callback_query(F.data.startswith("admin_confirm:"))
async def admin_confirm_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("غير مصرح", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    await deliver_order(order_id)
    await callback.message.edit_text(callback.message.text + "\n\n✅ تم التأكيد والتسليم.")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_reject:"))
async def admin_reject_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("غير مصرح", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    order = get_order(order_id=order_id)
    set_order_status(order_id, "rejected")
    await bot.send_message(order["user_id"], "❌ لم يتم تأكيد دفعتك. تواصل مع الدعم إذا كنت متأكد من التحويل.")
    await callback.message.edit_text(callback.message.text + "\n\n❌ تم الرفض.")
    await callback.answer()


class AddProduct(StatesGroup):
    name = State()
    description = State()
    price = State()
    category = State()


class AddStock(StatesGroup):
    product_id = State()
    content = State()


def admin_only(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID


@router.message(Command("admin"))
async def admin_menu(message: Message):
    if not admin_only(message):
        return
    await message.answer(
        "🛠️ لوحة الأدمن:\n"
        "/addproduct — إضافة منتج جديد\n"
        "/addstock — إضافة مخزون (حسابات/أكواد) لمنتج\n"
        "/products — عرض كل المنتجات وأرقامها\n"
        "/orders — عرض آخر الطلبات"
    )


@router.message(Command("products"))
async def products_cmd(message: Message):
    if not admin_only(message):
        return
    products = list_products()
    if not products:
        await message.answer("لا توجد منتجات.")
        return
    text = "\n".join(f"#{p['id']} - {p['name']} - {p['price']}$ - متوفر: {p['available']}" for p in products)
    await message.answer(text)


@router.message(Command("orders"))
async def orders_cmd(message: Message):
    if not admin_only(message):
        return
    conn = db()
    rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    if not rows:
        await message.answer("لا توجد طلبات بعد.")
        return
    text = "\n".join(f"#{r['id']} - منتج {r['product_id']} - {r['status']} - {r['method']}" for r in rows)
    await message.answer(text)


@router.message(Command("addproduct"))
async def addproduct_start(message: Message, state: FSMContext):
    if not admin_only(message):
        return
    await state.set_state(AddProduct.name)
    await message.answer("📝 اكتب اسم المنتج:")


@router.message(AddProduct.name)
async def addproduct_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(AddProduct.description)
    await message.answer("📝 اكتب وصف قصير للمنتج:")


@router.message(AddProduct.description)
async def addproduct_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(AddProduct.price)
    await message.answer("💵 اكتب السعر بالدولار (مثال: 5.5):")


@router.message(AddProduct.price)
async def addproduct_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("⚠️ اكتب رقم صحيح للسعر، مثال: 5.5")
        return
    await state.update_data(price=price)
    await state.set_state(AddProduct.category)
    await message.answer("🏷️ اكتب التصنيف (مثال: حسابات / أكواد سوفتوير):")


@router.message(AddProduct.category)
async def addproduct_category(message: Message, state: FSMContext):
    data = await state.update_data(category=message.text)
    conn = db()
    conn.execute(
        "INSERT INTO products (name, description, price, category) VALUES (?,?,?,?)",
        (data["name"], data["description"], data["price"], data["category"]),
    )
    conn.commit()
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    await state.clear()
    await message.answer(f"✅ تمت إضافة المنتج #{pid}\nالآن أضف له مخزون عبر /addstock")


@router.message(Command("addstock"))
async def addstock_start(message: Message, state: FSMContext):
    if not admin_only(message):
        return
    products = list_products()
    if not products:
        await message.answer("أضف منتج أولاً عبر /addproduct")
        return
    text = "اكتب رقم المنتج اللي تبي تضيف له مخزون:\n" + "\n".join(f"#{p['id']} - {p['name']}" for p in products)
    await state.set_state(AddStock.product_id)
    await message.answer(text)


@router.message(AddStock.product_id)
async def addstock_pid(message: Message, state: FSMContext):
    try:
        pid = int(message.text.strip().lstrip("#"))
    except ValueError:
        await message.answer("⚠️ اكتب رقم المنتج فقط، مثال: 1")
        return
    if not get_product(pid):
        await message.answer("⚠️ رقم منتج غير موجود.")
        return
    await state.update_data(product_id=pid)
    await state.set_state(AddStock.content)
    await message.answer(
        "📦 الآن أرسل محتوى المخزون. كل سطر = وحدة واحدة تُسلَّم لعميل واحد.\n\n"
        "مثال:\nuser1:pass1\nuser2:pass2\n\nأو لو الحساب سطر واحد فقط ارسل سطر واحد."
    )


@router.message(AddStock.content)
async def addstock_content(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data["product_id"]
    lines = [l.strip() for l in message.text.splitlines() if l.strip()]
    conn = db()
    for line in lines:
        conn.execute("INSERT INTO stock (product_id, content) VALUES (?,?)", (pid, line))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"✅ تمت إضافة {len(lines)} وحدة مخزون للمنتج #{pid}")


# ------------------------------------------------------------------
# خادم الويب (Mini App API + استقبال CryptoBot webhook)
# ------------------------------------------------------------------
async def handle_index(request):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "webapp", "index.html"))


async def handle_api_products(request):
    return web.json_response(list_products())


async def handle_api_order(request):
    body = await request.json()
    init_data = body.get("initData", "")
    user = validate_init_data(init_data)
    if not user:
        return web.json_response({"ok": False, "error": "بيانات تليكرام غير صالحة"}, status=403)
    product_id = int(body.get("product_id"))
    method = body.get("method")
    product = get_product(product_id)
    if not product:
        return web.json_response({"ok": False, "error": "المنتج غير موجود"}, status=404)

    if method == "cryptobot":
        if not CRYPTO_PAY_TOKEN:
            return web.json_response({"ok": False, "error": "CryptoBot غير مفعل"}, status=400)
        try:
            invoice = await cryptobot_create_invoice(product["price"], product["name"])
        except Exception:
            return web.json_response({"ok": False, "error": "تعذر إنشاء فاتورة الدفع"}, status=500)
        order_id = create_order(user["id"], product_id, None, product["price"], "cryptobot", invoice_id=str(invoice["invoice_id"]))
        return web.json_response({"ok": True, "order_id": order_id, "pay_url": invoice["bot_invoice_url"]})

    elif method == "binance":
        order_id = create_order(user["id"], product_id, None, product["price"], "binance", status="awaiting_payment")
        return web.json_response({"ok": True, "order_id": order_id, "binance_id": BINANCE_PAY_ID})

    return web.json_response({"ok": False, "error": "طريقة دفع غير معروفة"}, status=400)


async def handle_api_binance_confirm(request):
    """المستخدم يضغط 'لقد دفعت' من الميني آب"""
    body = await request.json()
    user = validate_init_data(body.get("initData", ""))
    if not user:
        return web.json_response({"ok": False}, status=403)
    order_id = int(body.get("order_id"))
    order = get_order(order_id=order_id)
    if not order or order["user_id"] != user["id"]:
        return web.json_response({"ok": False}, status=404)
    set_order_status(order_id, "pending_review")
    product = get_product(order["product_id"])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تأكيد الدفع وتسليم", callback_data=f"admin_confirm:{order_id}"),
                InlineKeyboardButton(text="❌ رفض", callback_data=f"admin_reject:{order_id}"),
            ]
        ]
    )
    await bot.send_message(
        ADMIN_ID,
        f"🔔 طلب دفع Binance Pay جديد #{order_id}\nالمنتج: {product['name']}\nالمبلغ: {order['amount']}$\n"
        f"العميل: <a href='tg://user?id={order['user_id']}'>{order['user_id']}</a>",
        reply_markup=kb,
    )
    return web.json_response({"ok": True})


async def handle_cryptobot_webhook(request):
    raw = await request.read()
    signature = request.headers.get("crypto-pay-api-signature", "")
    if not verify_cryptobot_signature(raw, signature):
        return web.json_response({"ok": False}, status=403)
    data = json.loads(raw)
    if data.get("update_type") == "invoice_paid":
        invoice = data["payload"]
        order = get_order(invoice_id=str(invoice["invoice_id"]))
        if order and order["status"] != "delivered":
            await deliver_order(order["id"])
    return web.json_response({"ok": True})


async def on_startup(app):
    init_db()
    asyncio.create_task(dp.start_polling(bot))
    log.info("Bot polling started")


def create_app():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/products", handle_api_products)
    app.router.add_post("/api/order", handle_api_order)
    app.router.add_post("/api/binance-confirm", handle_api_binance_confirm)
    app.router.add_post("/webhook/cryptobot", handle_cryptobot_webhook)
    app.router.add_static("/static/", os.path.join(os.path.dirname(__file__), "webapp"))
    app.on_startup.append(on_startup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
