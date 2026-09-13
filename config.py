import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))
DB_URL = os.getenv("DB_URL", "sqlite+aiosqlite:///bot.db")
PROXY_URL = os.getenv("PROXY_URL", "http://127.0.0.1:10809")
CHECK_DELETED_INTERVAL = int(os.getenv("CHECK_DELETED_INTERVAL", "30"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# WebSocket Server configuration
OD_SECRET_TOKEN = os.getenv("OD_SECRET_TOKEN", "")
WS_HOST = os.getenv("WS_HOST", "0.0.0.0")
WS_PORT = int(os.getenv("WS_PORT", "8080"))
SSL_KEYFILE = os.getenv("SSL_KEYFILE", "key.pem")
SSL_CERTFILE = os.getenv("SSL_CERTFILE", "cert.pem")

KEYWORDS = [
    "МГ",
    "ОВЧ",
    "Радиосеть",
    "Ретранслятор",
    "Алгоритм",
]
