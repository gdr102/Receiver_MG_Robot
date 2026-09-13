import asyncio
import json
from datetime import datetime
import pytz
import uvicorn
import websockets

from config import OD_SECRET_TOKEN
from database import (
    async_session_maker,
    ensure_daily_table,
    find_and_update_edited_message,
    get_all_active_messages,
    get_all_date_table_names,
    get_db_stats,
    get_recent_messages,
    init_db,
    mark_daily_message_deleted,
    save_message,
    upsert_user,
)
from filters import contains_keywords
from handlers.messages import get_msk_datetimes
from server import app, manager


async def run_all_tests():
    print("=== 1. Testing Database & Dynamic Daily Tables ===")
    await init_db()

    # Test Moscow date helper
    sample_utc = datetime(2026, 9, 13, 15, 0, tzinfo=pytz.utc)
    table_name, date_str = get_msk_datetimes(sample_utc)
    assert table_name == "13.09.2026", f"Table name mismatch: {table_name}"
    assert date_str == "13.09.2026_18.00", f"Date format mismatch: {date_str}"
    print(f"[PASS] Date conversion verified: table='{table_name}', date='{date_str}'")

    # Test keyword filtering
    assert contains_keywords("Проверка МГ на частоте") is True
    assert contains_keywords("Ретранслятор запущен") is True
    assert contains_keywords("Радиосеть 2 активна") is True
    assert contains_keywords("Алгоритм подтвержден") is True
    assert contains_keywords("ОВЧ диапазон") is True
    assert contains_keywords("помогать всем") is False
    assert contains_keywords("обычное сообщение") is False
    print("[PASS] Keyword filtering verified.")

    # Test saving messages in day 1 (13.09.2026)
    async with async_session_maker() as session:
        user = await upsert_user(session, tg_id=111, username="operator_alpha")

    # Add 10 messages for 13.09.2026
    for i in range(1, 11):
        msg = await save_message(
            user_username="operator_alpha",
            date_str=f"13.09.2026_10.{i:02d}",
            text_content=f"Сообщение МГ #{i} от 13 сентября",
            message_id=100 + i,
            date_table_name="13.09.2026",
        )
        assert msg["edit"] == 0
        assert msg["delete"] == 0

    # Add 10 messages for day 2 (14.09.2026) - simulating next day
    for i in range(1, 11):
        msg = await save_message(
            user_username="operator_alpha",
            date_str=f"14.09.2026_11.{i:02d}",
            text_content=f"Сообщение МГ #{i} от 14 сентября",
            message_id=200 + i,
            date_table_name="14.09.2026",
        )
        assert msg["edit"] == 0
        assert msg["delete"] == 0

    # Verify tables created
    tables = await get_all_date_table_names()
    assert "14.09.2026" in tables, "Table 14.09.2026 not found"
    assert "13.09.2026" in tables, "Table 13.09.2026 not found"
    print(f"[PASS] Dynamic tables created: {tables}")

    # Test editing message from 13.09.2026
    edited = await find_and_update_edited_message(
        message_id=105,
        new_text="Сообщение МГ #5 Отредактировано",
    )
    assert edited is not None
    assert edited["edit"] == 1
    assert edited["message"] == "Сообщение МГ #5 Отредактировано"
    print("[PASS] Edited message updated with edit=1 in its daily table.")

    # Test deleting message from 14.09.2026
    deleted = await mark_daily_message_deleted(
        table_name="14.09.2026", message_id=205
    )
    assert deleted is not None
    assert deleted["delete"] == 1
    print("[PASS] Deleted message updated with delete=1 in its daily table.")

    # Test get_recent_messages (15 messages across both tables)
    recent = await get_recent_messages(limit=15)
    assert len(recent) == 15
    # Oldest among recent 15 should be message 106 from 13.09.2026
    assert recent[0]["message_id"] == 106
    # Newest should be message 210 from 14.09.2026
    assert recent[-1]["message_id"] == 210
    print(f"[PASS] Retrieved {len(recent)} recent messages in chronological order.")

    # Test stats
    stats = await get_db_stats()
    assert stats["tables_count"] == 2
    assert stats["msgs_count"] == 20
    assert stats["edited_count"] == 1
    assert stats["deleted_count"] == 1
    print(f"[PASS] DB stats verified: {stats}")

    print("\n=== 2. Testing WebSocket Server with Dynamic Tables ===")
    test_port = 8092
    config = uvicorn.Config(app=app, host="127.0.0.1", port=test_port, log_level="error")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.5)

    ws_url = f"ws://127.0.0.1:{test_port}/ws"

    # Test without token (rejected)
    try:
        async with websockets.connect(ws_url):
            assert False, "Should be rejected without token"
    except Exception:
        print("[PASS] Connection rejected without token.")

    # Test with valid token
    async with websockets.connect(f"{ws_url}?token={OD_SECRET_TOKEN}") as ws:
        init_raw = await ws.recv()
        init_json = json.loads(init_raw)
        assert init_json["type"] == "INIT_HISTORY"
        assert len(init_json["messages"]) == 15
        print(f"[PASS] Client connected and received INIT_HISTORY ({len(init_json['messages'])} msgs).")

        # Test broadcast NEW_MESSAGE
        new_event = {
            "type": "NEW_MESSAGE",
            "data": {
                "id": 11,
                "user": "operator_alpha",
                "date": "14.09.2026_18.20",
                "message": "Новая радиосеть активна",
                "message_id": 999,
                "edit": 0,
                "delete": 0,
            },
        }
        await manager.broadcast(new_event)
        received = json.loads(await ws.recv())
        assert received["type"] == "NEW_MESSAGE"
        assert received["data"]["message_id"] == 999
        print("[PASS] Broadcast NEW_MESSAGE received by client.")

        # Test broadcast MESSAGE_EDITED
        edit_event = {
            "type": "MESSAGE_EDITED",
            "data": {
                "id": 11,
                "user": "operator_alpha",
                "date": "14.09.2026_18.20",
                "message": "Новая радиосеть активна (правка)",
                "message_id": 999,
                "edit": 1,
                "delete": 0,
            },
        }
        await manager.broadcast(edit_event)
        received = json.loads(await ws.recv())
        assert received["type"] == "MESSAGE_EDITED"
        assert received["data"]["edit"] == 1
        print("[PASS] Broadcast MESSAGE_EDITED received by client.")

        # Test broadcast MESSAGE_DELETED
        del_event = {
            "type": "MESSAGE_DELETED",
            "data": {
                "id": 11,
                "user": "operator_alpha",
                "message_id": 999,
                "edit": 1,
                "delete": 1,
            },
        }
        await manager.broadcast(del_event)
        received = json.loads(await ws.recv())
        assert received["type"] == "MESSAGE_DELETED"
        assert received["data"]["delete"] == 1
        print("[PASS] Broadcast MESSAGE_DELETED received by client.")

    server.should_exit = True
    await server_task
    print("\nAll integration tests passed successfully!")


if __name__ == "__main__":
    asyncio.run(run_all_tests())
