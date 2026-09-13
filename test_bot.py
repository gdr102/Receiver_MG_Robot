import asyncio
import os
import re
from datetime import datetime
import pytz

from database import (
    Base,
    User,
    Message,
    async_session_maker,
    engine,
    init_db,
    save_message,
    update_edited_message,
    mark_message_deleted,
    get_message_by_message_id,
    upsert_user,
    get_active_messages,
)
from filters import contains_keywords
from handlers.messages import format_msk_date


async def run_tests():
    print('--- Running Integration Tests ---')

    # 1. Database initialization
    await init_db()
    print('[PASS] Database initialized.')

    # 2. Test format_msk_date
    sample_utc = datetime(2026, 9, 13, 14, 30, tzinfo=pytz.utc)
    msk_str = format_msk_date(sample_utc)
    assert msk_str == '13.09.2026_17.30', f'Date format mismatch: {msk_str}'
    print(f'[PASS] Date format verified: {msk_str}')

    # 3. Test keywords filter
    positive_samples = [
        'Сообщение с МГ',
        'Проверка МГ-1',
        'Тест ОВЧ радиосети',
        'Ретрансляторы работают штатно',
        'Выполняется заданный алгоритм',
        'РАДИОСЕТЬ 5',
        'ретранслятор 2',
    ]
    negative_samples = [
        'помогать другим людям',
        'ночная мгла',
        'довчера все было нормально',
        'просто привет',
        'обычный текст без триггеров',
    ]
    for s in positive_samples:
        assert contains_keywords(s) is True, f'Expected True for: {s}'
    for s in negative_samples:
        assert contains_keywords(s) is False, f'Expected False for: {s}'
    print('[PASS] Keyword filtering verified.')

    # 4. Test User and Message DB operations
    async with async_session_maker() as session:
        user = await upsert_user(session, tg_id=987654321, username='signalman')
        assert user.tg_id == 987654321
        assert user.username == 'signalman'

        # Save message
        date_str = '13.09.2026_17.30'
        text_orig = 'Проверка связи в радиосети'
        msg = await save_message(
            session=session,
            user_username=user.username,
            date_str=date_str,
            text=text_orig,
            message_id=50001,
        )
        assert msg.message_id == 50001
        assert msg.user == 'signalman'
        assert msg.edit == 0
        assert msg.delete == 0
        assert msg.message == text_orig

        # Edit message
        text_edited = 'Проверка связи в радиосети Отредактировано'
        edited_msg = await update_edited_message(session, 50001, text_edited)
        assert edited_msg is not None
        assert edited_msg.edit == 1
        assert edited_msg.delete == 0
        assert edited_msg.message == text_edited

        # Delete message
        deleted_msg = await mark_message_deleted(session, 50001)
        assert deleted_msg is not None
        assert deleted_msg.edit == 1
        assert deleted_msg.delete == 1

        # Check get_active_messages (should not include deleted message)
        active = await get_active_messages(session)
        assert not any(m.message_id == 50001 for m in active)

    print('[PASS] User & Message CRUD, edit flag and delete flag verified.')
    print('All tests completed successfully!')


if __name__ == '__main__':
    asyncio.run(run_tests())
