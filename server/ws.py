import asyncio
import json
import logging
import os
from datetime import datetime
import pytz
from fastapi import FastAPI, Header, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

from config import OD_SECRET_TOKEN, SSL_CERTFILE, SSL_KEYFILE, TIMEZONE, WS_HOST, WS_PORT
from database import (
    get_all_date_table_names,
    get_messages_for_date,
    get_messages_last_24h,
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


def extract_and_verify_token(
    token_query: str | None = None,
    authorization: str | None = None,
    x_api_token: str | None = None,
) -> bool:
    """Checks secret token from query string, Authorization Bearer header, or X-API-Token header."""
    candidate = token_query
    if not candidate and x_api_token:
        candidate = x_api_token.strip()
    if not candidate and authorization:
        parts = authorization.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            candidate = parts[1].strip()
        else:
            candidate = authorization.strip()
    return bool(candidate and candidate == OD_SECRET_TOKEN)


class ConnectionManager:
    """Manages active WebSocket and SSE connections and real-time broadcasting."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self.sse_queues: list[asyncio.Queue] = []

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
        """Sends JSON event (NEW_MESSAGE, MESSAGE_EDITED, MESSAGE_DELETED) to all connected clients."""
        for conn in list(self.active_connections):
            try:
                await conn.send_json(message)
            except Exception as e:
                logger.warning(f"Failed to send to client ({e}), dropping connection.")
                self.disconnect(conn)

        for queue in list(self.sse_queues):
            try:
                queue.put_nowait(message)
            except Exception:
                if queue in self.sse_queues:
                    self.sse_queues.remove(queue)


manager = ConnectionManager()
app = FastAPI(title="Receiver MG Secure WebSocket & REST API Server")


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = Query(default=None),
    mode: str | None = Query(default=None),
):
    # 1. Token authentication BEFORE accepting connection
    auth_header = websocket.headers.get("authorization")
    api_token_header = websocket.headers.get("x-api-token")
    if not extract_and_verify_token(token, auth_header, api_token_header):
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
        if mode == "24h":
            # Режим 24h: сразу отдаем радиограммы за последние 24 часа
            data_24h = await get_messages_last_24h(include_deleted=False)
            await websocket.send_json(
                {
                    "type": "LAST_24H_MESSAGES",
                    **data_24h,
                }
            )
        else:
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

        # 6. Слушаем запросы клиента и heartbeat
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

                elif action == "GET_LAST_24H":
                    inc_del = bool(msg.get("include_deleted", False))
                    data_24h = await get_messages_last_24h(include_deleted=inc_del)
                    await websocket.send_json(
                        {
                            "type": "LAST_24H_MESSAGES",
                            **data_24h,
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


@app.websocket("/ws/24h")
async def websocket_24h_endpoint(
    websocket: WebSocket,
    token: str | None = Query(default=None),
):
    """
    Специализированный WebSocket-эндпоинт для внешних проектов:
    При подключении сразу отдает радиограммы за последние 24 часа (LAST_24H_MESSAGES),
    а затем в реальном времени транслирует события NEW_MESSAGE, MESSAGE_EDITED, MESSAGE_DELETED.
    """
    await websocket_endpoint(websocket=websocket, token=token, mode="24h")


# ---------------------------------------------------------------------------
# REST API эндпоинты
# ---------------------------------------------------------------------------


@app.get("/api/messages/24h")
@app.get("/api/radiograms/24h")
async def api_get_messages_last_24h(
    token: str | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    authorization: str | None = Header(default=None),
    x_api_token: str | None = Header(default=None),
):
    """
    Возвращает все радиограммы за последние 24 часа с момента текущего запроса (по МСК).
    По умолчанию возвращает только активные (delete=0). Если передать include_deleted=true,
    вернет включая удаленные (delete=1).
    """
    if not extract_and_verify_token(token, authorization, x_api_token):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    data_24h = await get_messages_last_24h(include_deleted=include_deleted)
    return data_24h


@app.get("/api/messages/stream")
async def api_stream_events(
    request: Request,
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    x_api_token: str | None = Header(default=None),
):
    """
    Server-Sent Events (SSE) поток для отслеживания добавления (NEW_MESSAGE),
    редактирования (MESSAGE_EDITED) и удаления (MESSAGE_DELETED) радиограмм в реальном времени.
    """
    if not extract_and_verify_token(token, authorization, x_api_token):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    queue: asyncio.Queue = asyncio.Queue()
    manager.sse_queues.append(queue)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in manager.sse_queues:
                manager.sse_queues.remove(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/dates")
async def api_get_dates(
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    x_api_token: str | None = Header(default=None),
):
    if not extract_and_verify_token(token, authorization, x_api_token):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    dates = await get_all_date_table_names()
    today_str = get_today_msk()
    if today_str not in dates:
        dates.insert(0, today_str)
    return {"dates": dates, "today": today_str}


@app.get("/api/day-messages")
async def api_get_day_messages(
    date: str,
    token: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    x_api_token: str | None = Header(default=None),
):
    if not extract_and_verify_token(token, authorization, x_api_token):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
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
