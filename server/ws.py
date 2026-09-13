import asyncio
import logging
import os
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, status
import uvicorn

from config import OD_SECRET_TOKEN, SSL_CERTFILE, SSL_KEYFILE, WS_HOST, WS_PORT
from database import async_session_maker, get_recent_messages

logger = logging.getLogger("websocket_server")


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
        # Close immediately with 4003 (Policy Violation / Unauthorized) before handshake accept
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION if hasattr(status, "WS_1008_POLICY_VIOLATION") else 4003,
            reason="Unauthorized: Invalid or missing secret token",
        )
        return

    # 2. Accept connection
    await manager.connect(websocket)

    # 3. Send INIT_HISTORY with last 15 messages
    try:
        async with async_session_maker() as session:
            recent_msgs = await get_recent_messages(session, limit=15)

        await websocket.send_json(
            {
                "type": "INIT_HISTORY",
                "messages": recent_msgs,
            }
        )

        # 4. Keep alive / receive client messages
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        logger.info("Client disconnected normally.")
    except Exception as e:
        logger.debug(f"Client connection closed with exception: {e}")
    finally:
        manager.disconnect(websocket)


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
