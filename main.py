import os
import json
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BITRIX_WEBHOOK = os.environ["BITRIX_WEBHOOK"]

CACHE_FILE = "/data/notified_cache.json"

TARGET_STAGES = {
    # Дизайн/Архитектура Бишкек
    "Новая заявка",
    "Лид Квалифицирован",
    "Встреча назначена",
    "Встреча состоялась",
    "Предварительно Да",
    "Сделка провалена",
    "Сделка успешна",
    # Воронка Казахстан
    "Новая заявка получена",
    "Квалификация пройдена",
    "Встреча проведена",
    "Закрыто и не реализовано",
}

STAGE_EMOJI = {
    "Новая заявка": "1️⃣",
    "Новая заявка получена": "1️⃣",
    "Лид Квалифицирован": "2️⃣",
    "Квалификация пройдена": "2️⃣",
    "Встреча назначена": "3️⃣",
    "Встреча состоялась": "4️⃣",
    "Встреча проведена": "4️⃣",
    "Предварительно Да": "5️⃣",
    "Сделка провалена": "❌",
    "Закрыто и не реализовано": "❌",
    "Сделка успешна": "🏆",
}

AMOUNT_STAGES = {"Сделка успешна"}

ALLOWED_SITE_SOURCES = {
    "Заявка Бишкек с сайта Артём",
    "Заявка Алматы с сайта Артём",
}

STAGE_TO_FUNNEL = {
    "Новая заявка": "Бишкек",
    "Лид Квалифицирован": "Бишкек",
    "Встреча назначена": "Бишкек",
    "Встреча состоялась": "Бишкек",
    "Предварительно Да": "Бишкек",
    "Сделка провалена": "Бишкек",
    "Сделка успешна": "Бишкек",
    "Новая заявка получена": "Казахстан",
    "Квалификация пройдена": "Казахстан",
    "Встреча проведена": "Казахстан",
    "Закрыто и не реализовано": "Казахстан",
}

SOURCE_MAP = {
    "996558551058": "🇰🇬 WhatsApp Кыргызстан 996558551058",
    "77009444243": "🇰🇿 WhatsApp Алматы 77009444243",
    "77775901319": "🇰🇿 WhatsApp Астана 77775901319",
    "7 777 590 1319": "🇰🇿 WhatsApp Астана 77775901319",
}


def load_cache() -> tuple[set, dict]:
    try:
        with open(CACHE_FILE, "r") as f:
            raw = json.load(f)
            if isinstance(raw, list):
                return set(raw), {}
            return set(raw.get("keys", [])), raw.get("funnels", {})
    except Exception:
        return set(), {}


def save_cache(keys: set, funnels: dict):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump({"keys": list(keys), "funnels": funnels}, f)
    except Exception:
        logger.exception("Failed to save cache")


def detect_source(title: str, source_desc: str = "") -> str | None:
    text = f"{title} {source_desc}"
    for key, label in SOURCE_MAP.items():
        if key in text:
            return label
    return None


def format_amount(amount: str, currency: str) -> str:
    try:
        value = float(amount)
        formatted = f"{value:,.0f}".replace(",", " ")
        return f"{formatted} {currency}"
    except Exception:
        return f"{amount} {currency}"


async def get_site_source_name(source_id: str) -> str | None:
    if not source_id:
        return None
    statuses = await bitrix_call("crm.status.list", {"filter[ENTITY_ID]": "SOURCE"})
    if isinstance(statuses, list):
        for s in statuses:
            if s.get("STATUS_ID") == source_id:
                name = s.get("NAME", "")
                return name if name in ALLOWED_SITE_SOURCES else None
    return None


async def bitrix_call(method: str, params: dict) -> dict | list:
    url = f"{BITRIX_WEBHOOK}/{method}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, data=params)
        r.raise_for_status()
        return r.json().get("result", {})


async def get_stage_name(stage_id: str, category_id: str) -> str:
    stages = await bitrix_call("crm.dealcategory.stage.list", {"id": category_id})
    if isinstance(stages, list):
        for s in stages:
            if s.get("STATUS_ID") == stage_id:
                return s.get("NAME", stage_id)
    return stage_id


async def get_manager_name(user_id: str) -> str:
    if not user_id:
        return "—"
    users = await bitrix_call("user.get", {"ID": user_id})
    if isinstance(users, list) and users:
        u = users[0]
        return f"{u.get('NAME', '')} {u.get('LAST_NAME', '')}".strip()
    return f"ID {user_id}"


async def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        })
        r.raise_for_status()


@app.post("/webhook")
async def webhook(request: Request):
    try:
        form = await request.form()
        data = dict(form)
        logger.info(f"Received webhook: {data}")

        deal_id = data.get("deal_id") or data.get("data[FIELDS][ID]")
        if not deal_id:
            doc = data.get("document_id[2]", "")
            if doc.startswith("DEAL_"):
                deal_id = doc.replace("DEAL_", "")
        if not deal_id:
            return JSONResponse({"ok": True, "skip": "no deal id"})

        deal = await bitrix_call("crm.deal.get", {"id": deal_id})
        if not deal:
            return JSONResponse({"ok": True, "skip": "deal not found"})

        stage_id = deal.get("STAGE_ID", "")
        category_id = deal.get("CATEGORY_ID", "0")
        stage_name = await get_stage_name(stage_id, category_id)

        if stage_name not in TARGET_STAGES:
            logger.info(f"SKIP stage: '{stage_name}'")
            return JSONResponse({"ok": True, "skip": "stage not watched"})

        cache_key = f"{deal_id}:{stage_name}"
        cache, funnels = load_cache()
        if cache_key in cache:
            logger.info(f"SKIP duplicate: deal {deal_id}, stage '{stage_name}'")
            return JSONResponse({"ok": True, "skip": "duplicate"})

        title = deal.get("TITLE", "—")
        source_desc = deal.get("SOURCE_DESCRIPTION", "")
        source_id = deal.get("SOURCE_ID", "")
        source = detect_source(title, source_desc)
        if source is None:
            source = await get_site_source_name(source_id)
        if source is None:
            logger.info(f"SKIP source not found: title='{title}', SOURCE_ID='{source_id}'")
            return JSONResponse({"ok": True, "skip": "source not in watch list"})

        client_name = title.split(" - ")[0].strip() if " - " in title else title

        assigned_id = deal.get("ASSIGNED_BY_ID", "")
        first = deal.get("ASSIGNED_BY_NAME", "")
        last = deal.get("ASSIGNED_BY_LAST_NAME", "")
        manager = f"{first} {last}".strip() or await get_manager_name(assigned_id)

        phone = "—"
        contact_id = deal.get("CONTACT_ID")
        if contact_id:
            contact = await bitrix_call("crm.contact.get", {"id": contact_id})
            phones = contact.get("PHONE", [])
            if phones:
                phone = phones[0].get("VALUE", "—")

        deal_link = f"https://neo-style.bitrix24.ru/crm/deal/details/{deal_id}/"
        emoji = STAGE_EMOJI.get(stage_name, "🔔")

        curr_funnel = STAGE_TO_FUNNEL.get(stage_name, "")
        prev_funnel = funnels.get(deal_id, "")
        if prev_funnel and prev_funnel != curr_funnel:
            source_header = f"{source}\n📂 <i>{prev_funnel} ➡️ {curr_funnel}</i>"
        else:
            source_header = source

        msg = (
            f"<b>{source_header}</b>\n\n"
            f"{emoji} <b>этап:</b> {stage_name}\n"
            f"👤<b>Клиент:</b> {client_name}\n"
            f"☎️<b>Телефон:</b> {phone}\n"
        )

        if stage_name in AMOUNT_STAGES:
            amount = deal.get("OPPORTUNITY", "")
            currency = deal.get("CURRENCY_ID", "")
            if amount:
                msg += f"💰<b>Сумма:</b> {format_amount(amount, currency)}\n"

        msg += f"\n<b>Ссылка на сделку:</b> {deal_link}"

        await send_telegram(msg)
        cache.add(cache_key)
        if curr_funnel:
            funnels[deal_id] = curr_funnel
        save_cache(cache, funnels)
        logger.info(f"Notification sent for deal {deal_id}, stage: {stage_name}")
        return JSONResponse({"ok": True})

    except Exception as e:
        logger.exception("Webhook error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.get("/")
async def health():
    return {"status": "running"}
