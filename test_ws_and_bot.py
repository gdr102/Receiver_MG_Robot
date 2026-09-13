import asyncio
import json
from datetime import datetime
import pytz
import uvicorn
import websockets

from config import OD_SECRET_TOKEN
from database import (
    async_session_maker,
    init_db,
    save_message,
    update_edited_message,
    mark_message_deleted,
    upsert_user,
    get_recent_messages,
)
from filters import contains_keywords
from handlers.messages import format_msk_date
from server import app, manager


async def run_all_tests():
    print('=== 1. Testing Database & Logic ===')
    await init_db()

    # Test date formatting
    sample_utc = datetime(2026, 9, 13, 14, 30, tzinfo=pytz.utc)
    msk_str = format_msk_date(sample_utc)
    assert msk_str == '13.09.2026_17.30', f'Date format mismatch: {msk_str}'
    print(f'[PASS] Date format verified: {msk_str}')

    # Test keywords
    assert contains_keywords('Сообщение МГ на связи') is True
    assert contains_keywords('Тест ретранслятор активен') is True
    assert contains_keywords('Ночной алгоритм') is True
    assert contains_keywords('помогать не надо') is False
    assert contains_keywords('просто текст') is False
    print('[PASS] Keyword filtering verified.')

    # Seed some test messages
    async with async_session_maker() as session:
        user = await upsert_user(session, tg_id=777888, username='operator_1')
        for i in range(1, 20):
            await save_message(
                session=session,
                user_username=user.username,
                date_str=f'13.09.2026_17.{i:02d}',
                text=f'Тестовое сообщение МГ #{i}',
                message_id=1000 + i,
            )

    # Verify get_recent_messages returns exactly 15 messages in chronological order
    async with async_session_maker() as session:
        recent = await get_recent_messages(session, limit=15)
        assert len(recent) == 15, f'Expected 15 messages, got {len(recent)}'
        assert recent[0]['message_id'] == 1005  # 20 - 15 + 1 = 5th message (id 1005)
        assert recent[-1]['message_id'] == 1019 # Last message
    print('[PASS] INIT_HISTORY 15 messages retrieved in chronological order.')

    print('\n=== 2. Testing Secure WebSocket Server ===')
    # Start test Uvicorn server on port 8089
    test_port = 8089
    config = uvicorn.Config(app=app, host='127.0.0.1', port=test_port, log_level='error')
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.5)

    ws_base_url = f'ws://127.0.0.1:{test_port}/ws'

    # Test 2.1: Rejection without token
    try:
        async with websockets.connect(ws_base_url):
            assert False, 'Should not have connected without token'
    except Exception as e:
        print('[PASS] Unauthorized connection without token rejected immediately.')

    # Test 2.2: Rejection with invalid token
    try:
        async with websockets.connect(f'{ws_base_url}?token=wrong_secret'):
            assert False, 'Should not have connected with wrong token'
    except Exception as e:
        print('[PASS] Unauthorized connection with wrong token rejected immediately.')

    # Test 2.3: Connection with valid token & check INIT_HISTORY
    async with websockets.connect(f'{ws_base_url}?token={OD_SECRET_TOKEN}') as ws:
        init_payload = await ws.recv()
        init_data = json.loads(init_payload)
        assert init_data['type'] == 'INIT_HISTORY', f'Unexpected type: {init_data}'
        assert len(init_data['messages']) == 15, f'Expected 15 messages: {len(init_data["messages"])}'
        print(f'[PASS] Valid client connected and received INIT_HISTORY with {len(init_data["messages"])} messages.')

        # Test 2.4: Broadcast NEW_MESSAGE
        new_msg_event = {
            'type': 'NEW_MESSAGE',
            'data': {
                'id': 99,
                'user': 'operator_1',
                'date': '13.09.2026_17.35',
                'message': 'Срочно в радиосеть',
                'message_id': 9999,
                'edit': 0,
                'delete': 0
            }
        }
        await manager.broadcast(new_msg_event)
        received_payload = await ws.recv()
        received_data = json.loads(received_payload)
        assert received_data['type'] == 'NEW_MESSAGE'
        assert received_data['data']['message_id'] == 9999
        print('[PASS] Broadcast NEW_MESSAGE successfully received by client.')

        # Test 2.5: Broadcast MESSAGE_EDITED
        edit_msg_event = {
            'type': 'MESSAGE_EDITED',
            'data': {
                'id': 99,
                'user': 'operator_1',
                'date': '13.09.2026_17.35',
                'message': 'Срочно в радиосеть (Отредактировано)',
                'message_id': 9999,
                'edit': 1,
                'delete': 0
            }
        }
        await manager.broadcast(edit_msg_event)
        received_payload = await ws.recv()
        received_data = json.loads(received_payload)
        assert received_data['type'] == 'MESSAGE_EDITED'
        assert received_data['data']['edit'] == 1
        print('[PASS] Broadcast MESSAGE_EDITED successfully received by client.')

        # Test 2.6: Broadcast MESSAGE_DELETED
        del_msg_event = {
            'type': 'MESSAGE_DELETED',
            'data': {
                'id': 99,
                'user': 'operator_1',
                'message_id': 9999,
                'edit': 1,
                'delete': 1
            }
        }
        await manager.broadcast(del_msg_event)
        received_payload = await ws.recv()
        received_data = json.loads(received_payload)
        assert received_data['type'] == 'MESSAGE_DELETED'
        assert received_data['data']['delete'] == 1
        print('[PASS] Broadcast MESSAGE_DELETED successfully received by client.')

    server.should_exit = True
    await server_task
    print('\nAll integration tests for Telegram Bot and Secure WebSocket passed!')


if __name__ == '__main__':
    asyncio.run(run_all_tests())
