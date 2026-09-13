# Receiver_MG_Robot & WSS WebSocket Server

Полностью асинхронный проект на Python (aiogram 3.x, FastAPI, Uvicorn, SQLAlchemy 2 / aiosqlite).

## 🔐 Генерация SSL для WSS на VPS
openssl req -x509 -newkey rsa:4096 -nodes -keyout key.pem -out cert.pem -days 365 -subj "/CN=localhost"

## 🚀 Запуск
1. pip install -r requirements.txt
2. python test_ws_and_bot.py
3. python main.py
