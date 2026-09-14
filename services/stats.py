import logging
import re
from collections import defaultdict
from datetime import datetime
from aiogram.utils.formatting import Bold, Code, Text, as_list

logger = logging.getLogger(__name__)


def extract_network(text: str) -> str:
    """
    Extracts radio network / unit name from message text.
    Examples:
      'Радиосеть 1 шб 22 ошбр.' -> '1 шб 22 ошбр'
      'Радиосеть БТГр 222 омбр.' -> 'БТГр 222 омбр'
      'Радиосеть 212 омбр.' -> '212 омбр'
      'Радиосеть н/у подразделения.' -> 'н/у подразделения'
    """
    if not text:
        return "н/у подразделения"

    m = re.search(
        r"(?:радиосеть|радиосети|радиосетей)\s*[:№\-]?\s*([^.\n\r]+)",
        text,
        re.IGNORECASE,
    )
    if m:
        val = m.group(1).strip()
        val = re.sub(r"[\.,;]+$", "", val).strip()
        if val:
            return val
    return "н/у подразделения"


def parse_period(text: str) -> tuple[datetime, datetime] | None:
    """
    Parses datetime period string in format: 'dd.mm.yyyy HH.MM - dd.mm.yyyy HH.MM'.
    Accepts dots, colons, underscores and hyphens/dashes.
    """
    cleaned = text.strip().strip('"\'«»')
    pattern = (
        r"(\d{2}\.\d{2}\.\d{4})\s*[_ ]\s*(\d{2}[.:]\d{2})\s*[-—–]\s*"
        r"(\d{2}\.\d{2}\.\d{4})\s*[_ ]\s*(\d{2}[.:]\d{2})"
    )
    m = re.search(pattern, cleaned)
    if not m:
        return None

    d1, t1, d2, t2 = m.groups()
    t1 = t1.replace(":", ".")
    t2 = t2.replace(":", ".")

    try:
        dt1 = datetime.strptime(f"{d1} {t1}", "%d.%m.%Y %H.%M")
        dt2 = datetime.strptime(f"{d2} {t2}", "%d.%m.%Y %H.%M")
        if dt1 > dt2:
            dt1, dt2 = dt2, dt1
        return dt1, dt2
    except ValueError:
        return None


def create_stats_report(
    period_str: str,
    messages: list[dict],
) -> Text:
    """
    Builds the statistics table using aiogram Rich Messages (aiogram.utils.formatting).
    The table columns are aligned with box characters, headers are formatted with Bold(),
    and digits are wrapped in Code() so that tapping/clicking on the number in Telegram
    immediately copies it to the clipboard.
    """
    # Group messages by extracted radio network
    unit_groups: dict[str, list[dict]] = defaultdict(list)
    for msg in messages:
        unit = extract_network(msg["message"])
        unit_groups[unit].append(msg)

    # Sort units descending by messages count
    sorted_units = sorted(
        unit_groups.items(), key=lambda item: len(item[1]), reverse=True
    )

    # Dynamic padding for column width
    max_unit_len = max([len(unit) for unit, _ in sorted_units] + [len("подразделение")])
    unit_col_width = max(max_unit_len, 14)

    border_top = f"┌{'─' * (unit_col_width + 2)}┬{'─' * 20}┐\n"
    border_mid = f"├{'─' * (unit_col_width + 2)}┼{'─' * 20}┤\n"
    border_bot = f"└{'─' * (unit_col_width + 2)}┴{'─' * 20}┘"

    header_padding = " " * (unit_col_width - len("подразделение"))
    header_elements = [
        "│ ",
        Bold("подразделение"),
        f"{header_padding} │ ",
        Bold("количество"),
        "          │\n",
    ]

    table_elements = [border_top, *header_elements, border_mid]

    for unit, msgs in sorted_units:
        count = len(msgs)
        unit_pad = " " * (unit_col_width - len(unit))
        raw_count_str = f"{count} радиограмм"
        count_pad = " " * max(0, 18 - len(raw_count_str))

        # Each row: unit name + Code(count) (clickable/copyable in Telegram)
        table_elements.extend([
            f"│ {unit}{unit_pad} │ ",
            Code(count),
            f" радиограмм{count_pad} │\n",
        ])

    table_elements.append(border_bot)
    table_rich_text = Text(*table_elements)

    # Wrap the whole message as a rich Text list
    content = as_list(
        f"Итого за период {period_str}",
        "",
        table_rich_text,
        "",
        'Чтобы получить статистику напишите период и время в формате "дд.мм.гггг чч.мм - дд.мм.гггг чч.мм".',
    )

    return content
