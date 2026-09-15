import html
import logging
import re
from collections import defaultdict
from datetime import datetime
from aiogram.types import InputRichMessage

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
) -> InputRichMessage:
    """
    Builds the statistics report as an InputRichMessage with a native table.
    Lists radio networks, message counts per network, and a 'Всего' total row.
    """
    unit_groups: dict[str, list[dict]] = defaultdict(list)
    total_count = len(messages)

    for msg in messages:
        unit = extract_network(msg["message"])
        unit_groups[unit].append(msg)

    sorted_units = sorted(
        unit_groups.items(), key=lambda item: len(item[1]), reverse=True
    )

    rows_html = []
    for unit, msgs in sorted_units:
        count = len(msgs)
        escaped_unit = html.escape(unit)
        rows_html.append(
            f"<tr><td>{escaped_unit}</td><td><code>{count}</code></td></tr>"
        )

    # Add 'Всего' summary row
    rows_html.append(
        f"<tr><td><b>Всего</b></td><td><b><code>{total_count}</code></b></td></tr>"
    )

    table_rows = "".join(rows_html)
    escaped_period = html.escape(period_str)

    html_content = (
        f"<p><b>Итого за период {escaped_period}</b></p>"
        f"<table>"
        f"<thead><tr><th>Радиосеть</th><th>Радиограмм</th></tr></thead>"
        f"<tbody>{table_rows}</tbody>"
        f"</table>"
        f'<p>Чтобы получить статистику напишите период и время в формате "дд.мм.гггг чч.мм - дд.мм.гггг чч.мм".</p>'
    )

    return InputRichMessage(html=html_content)
