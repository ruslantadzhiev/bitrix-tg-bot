import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BITRIX_WEBHOOK = os.environ["BITRIX_WEBHOOK"]  # https://neo-style.bitrix24.ru/rest/1/TOKEN

TARGET_STAGES = {"Новая заявка", "Лид Квалифицирован", "Встреча назначена"}

SOURCE_MAP = {
    "996558551058": "📱 WhatsApp Кыргызстан",
    "77009444243": "📱 WhatsApp Казахстан",
    "77775901319": "📱 WhatsApp Астана",
}


def detect_source(title: str) -> str:
    for key, label in SOURCE_MAP.items():
        if key in title:
            return label
    return "❓ Источник не определён"


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

        deal_id = data.get("data[FIELDS][ID]")
        if not deal_id:
            return JSONResponse({"ok": True, "skip": "no deal id"})

        deal = await bitrix_call("crm.deal.get", {"id": deal_id})
        if not deal:
            return JSONResponse({"ok": True, "skip": "deal not found"})

        stage_id = deal.get("STAGE_ID", "")
        category_id = deal.get("CATEGORY_ID", "0")
        stage_name = await get_stage_name(stage_id, category_id)

        if stage_name not in TARGET_STAGES:
            logger.info(f"Stage '{stage_name}' not in watch list, skipping")
            return JSONResponse({"ok": True, "skip": f"stage not watched"})

        title = deal.get("TITLE", "—")
        source = detect_source(title)
        client_name = title.split(" - ")[0].strip() if " - " in title else title

        assigned_id = deal.get("ASSIGNED_BY_ID", "")
        manager = await get_manager_name(assigned_id)

        # Phone from linked contact
        phone = "—"
        contact_id = deal.get("CONTACT_ID")
        if contact_id:
            contact = await bitrix_call("crm.contact.get", {"id": contact_id})
            phones = contact.get("PHONE", [])
            if phones:
                phone = phones[0].get("VALUE", "—")

        msg = (
            f"🔔 <b>Изменение этапа сделки</b>\n\n"
            f"{source}\n"
            f"👤 <b>Клиент:</b> {client_name}\n"
            f"📞 <b>Телефон:</b> {phone}\n"
            f"📍 <b>Этап:</b> {stage_name}\n"
            f"👨‍💼 <b>Менеджер:</b> {manager}"
        )

        await send_telegram(msg)
        logger.info(f"Notification sent for deal {deal_id}, stage: {stage_name}")
        return JSONResponse({"ok": True})

    except Exception as e:
        logger.exception("Webhook error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)


@app.get("/")
async def health():
    return {"status": "running"}
