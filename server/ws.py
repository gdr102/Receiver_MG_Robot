import asyncio
import json
import logging
import os
from datetime import datetime
import pytz
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, status
import uvicorn

from config import OD_SECRET_TOKEN, SSL_CERTFILE, SSL_KEYFILE, TIMEZONE, WS_HOST, WS_PORT
from database import (
    get_all_date_table_names,
    get_messages_for_date,
    get_recent_messages,
)

logger = logging.getLogger("websocket_server")


def get_today_msk() -> str:
    """Returns today's date in Moscow timezone (format 'dd.mm.yyyy')."""
    try:
        msk_tz = pytz.timezone(TIMEZONE)
        return datetime.now(msk_tz).strftime("%d.%m.%Y")
    except Exception:
        return datetime.now().strftime("%d.%m.%Y")


class ConnectionManager:
    """Manages active WebSocket connections and broadcasting."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(
            f"WebSocket client connected. Active connections: {len(self.active_connections)}"
        )

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(
                f"WebSocket client disconnected. Active connections: {len(self.active_connections)}"
            )

    async def broadcast(self, message: dict):
        """Sends JSON event to all connected clients."""
        if not self.active_connections:
            return

        for conn in list(self.active_connections):
            try:
                await conn.send_json(message)
            except Exception as e:
                logger.warning(f"Failed to send to client ({e}), dropping connection.")
                self.disconnect(conn)


manager = ConnectionManager()
app = FastAPI(title="Receiver MG Secure WebSocket Server")


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket, token: str | None = Query(default=None)
):
    # 1. Token authentication BEFORE accepting connection
    if not token or token != OD_SECRET_TOKEN:
        client_ip = websocket.client.host if websocket.client else "unknown"
        logger.warning(
            f"Unauthorized WebSocket attempt from {client_ip}. Rejecting with code 4003."
        )
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION if hasattr(status, "WS_1008_POLICY_VIOLATION") else 4003,
            reason="Unauthorized: Invalid or missing secret token",
        )
        return

    # 2. Accept connection
    await manager.connect(websocket)

    try:
        today_str = get_today_msk()
        all_dates = await get_all_date_table_names()
        if today_str not in all_dates:
            all_dates.insert(0, today_str)

        # 3. Отдаем список всех папок/дат
        await websocket.send_json(
            {
                "type": "AVAILABLE_DATES",
                "dates": all_dates,
                "today": today_str,
            }
        )

        # 4. Отдаем ВСЮ хронологию за сегодня (delete=0)
        today_messages = await get_messages_for_date(today_str)
        await websocket.send_json(
            {
                "type": "DAY_MESSAGES",
                "date": today_str,
                "messages": today_messages,
                "is_initial": True,
            }
        )

        # 5. Обратная совместимость с INIT_HISTORY
        recent_msgs = await get_recent_messages(limit=100)
        await websocket.send_json(
            {
                "type": "INIT_HISTORY",
                "messages": recent_msgs,
            }
        )

        # 6. Слушаем запросы на загрузку прошедших дней и heartbeat
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
                continue

            try:
                msg = json.loads(data)
                action = msg.get("action")

                if action == "GET_DATES":
                    dates = await get_all_date_table_names()
                    today_msk = get_today_msk()
                    if today_msk not in dates:
                        dates.insert(0, today_msk)
                    await websocket.send_json(
                        {
                            "type": "AVAILABLE_DATES",
                            "dates": dates,
                            "today": today_msk,
                        }
                    )

                elif action == "GET_DAY_MESSAGES":
                    req_date = msg.get("date")
                    if req_date:
                        day_msgs = await get_messages_for_date(req_date)
                        logger.info(
                            f"Client requested day '{req_date}'. Sent {len(day_msgs)} messages."
                        )
                        await websocket.send_json(
                            {
                                "type": "DAY_MESSAGES",
                                "date": req_date,
                                "messages": day_msgs,
                            }
                        )

            except json.JSONDecodeError:
                pass
            except Exception as e:
                logger.warning(f"Error handling WebSocket client action: {e}")

    except WebSocketDisconnect:
        logger.info("Client disconnected normally.")
    except Exception as e:
        logger.debug(f"Client connection closed with exception: {e}")
    finally:
        manager.disconnect(websocket)


# REST эндпоинты для списка дат и сообщений за день
@app.get("/api/dates")
async def api_get_dates(token: str | None = Query(default=None)):
    if token != OD_SECRET_TOKEN:
        return {"error": "Unauthorized"}
    dates = await get_all_date_table_names()
    today_str = get_today_msk()
    if today_str not in dates:
        dates.insert(0, today_str)
    return {"dates": dates, "today": today_str}


@app.get("/api/day-messages")
async def api_get_day_messages(date: str, token: str | None = Query(default=None)):
    if token != OD_SECRET_TOKEN:
        return {"error": "Unauthorized"}
    messages = await get_messages_for_date(date)
    return {"date": date, "count": len(messages), "messages": messages}


def create_ws_server() -> uvicorn.Server:
    """Configures and returns Uvicorn Server instance."""
    has_ssl = os.path.exists(SSL_KEYFILE) and os.path.exists(SSL_CERTFILE)

    kwargs = {
        "app": app,
        "host": WS_HOST,
        "port": WS_PORT,
        "log_level": "info",
    }

    if has_ssl:
        logger.info(
            f"🔒 SSL files found ({SSL_CERTFILE}, {SSL_KEYFILE}). WSS mode active: wss://{WS_HOST}:{WS_PORT}/ws"
        )
        kwargs["ssl_keyfile"] = SSL_KEYFILE
        kwargs["ssl_certfile"] = SSL_CERTFILE
    else:
        logger.warning(
            f"⚠️ SSL files '{SSL_KEYFILE}' or '{SSL_CERTFILE}' not found. WS mode active: ws://{WS_HOST}:{WS_PORT}/ws"
        )

    config = uvicorn.Config(**kwargs)
    return uvicorn.Server(config)
