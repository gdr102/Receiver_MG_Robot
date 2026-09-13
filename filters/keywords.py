import re
from aiogram.filters import BaseFilter
from aiogram.types import Message


def contains_keywords(text: str | None) -> bool:
    """
    Checks if text contains any of the target keywords:
    'МГ', 'ОВЧ', 'Радиосеть', 'Ретранслятор', 'Алгоритм'.
    Uses regex to avoid false positives (e.g. 'помогать', 'мгла') while
    matching inflected word forms and acronyms.
    """
    if not text:
        return False

    # Standalone acronyms: МГ, ОВЧ (e.g. 'МГ', 'мг-1', 'ОВЧ_2')
    acronym_pattern = r"(?<![а-яёa-z0-9])(?:мг|овч)(?![а-яёa-z0-9])"
    if re.search(acronym_pattern, text, re.IGNORECASE):
        return True

    # Words and inflected forms: Радиосеть, Ретранслятор, Алгоритм
    words_pattern = r"(?<![а-яёa-z0-9])(?:радиосет|ретранслятор|алгоритм)"
    if re.search(words_pattern, text, re.IGNORECASE):
        return True

    return False


class ContainsKeywordsFilter(BaseFilter):
    """aiogram filter to check if message contains specified keywords."""

    async def __call__(self, message: Message) -> bool:
        text = message.text or message.caption
        return contains_keywords(text)
