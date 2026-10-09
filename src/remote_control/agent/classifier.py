"""Automatic task classification and reasoning effort recommendation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class TaskProfile:
    """Classification profile of a user task."""

    task_type: Literal["action", "query", "complex"]
    effort: EffortLevel
    label: str
    description: str
    guidance: str


_COMPLEX_PATTERN = re.compile(
    r"\b(проанализируй|исследуй|сравни|рефакторинг|архитектур[а-я]*|почему|ошибк[а-я]*|почини|"
    r"исправь|найди баг|отлад[а-я]*|напиши тест[а-я]*|оптимизир[а-я]*|уязвимост[а-я]*|"
    r"security|audit|debug|refactor|benchmark|разберись)\b",
    re.IGNORECASE,
)

_QUERY_PATTERN = re.compile(
    r"\b(список|выведи список|покажи|найди|какие|какой|сколько|статус|информаци[а-я]*|"
    r"лог[а-я]*|файлы|директори[а-я]*|процессы|содержимое|прочитай|"
    r"get-|cat|ls|dir)\b",
    re.IGNORECASE,
)

_ACTION_PATTERN = re.compile(
    r"\b(запусти|открой|окно|закрой|убей|останови|создай|удали|перемести|скопируй|"
    r"перезапусти|включи|выключи|блокнот|notepad|steam|стим|браузер|chrome|"
    r"start|kill|stop|run|clean)\b",
    re.IGNORECASE,
)


def classify_task(prompt: str) -> TaskProfile:
    """Classify user task into an execution profile with recommended reasoning effort."""
    clean = prompt.strip().lower()

    # 1. Complex analytical, debugging or refactoring tasks
    if _COMPLEX_PATTERN.search(clean):
        return TaskProfile(
            task_type="complex",
            effort="high",
            label="🧠 Глубокий (high)",
            description="Анализ, отладка или кодинг",
            guidance=(
                "Тщательно проанализируй задачу, проверь детали и возможные ошибки. "
                "Предоставь аргументированный, структурированный и качественный результат."
            ),
        )

    # 2. Query or data listing tasks (when not explicitly opening an app window)
    if _QUERY_PATTERN.search(clean) and not _ACTION_PATTERN.search(clean):
        return TaskProfile(
            task_type="query",
            effort="low",
            label="📋 Сбор данных (low)",
            description="Поиск данных, списки или просмотр",
            guidance=(
                "Собери запрошенные данные и выведи их структурированно и наглядно "
                "(в виде таблицы или компактного списка). Не добавляй лишних вступительных слов."
            ),
        )

    # 3. Direct host action tasks (launching apps, creating files, toggling settings)
    return TaskProfile(
        task_type="action",
        effort="low",
        label="⚡ Быстрый (low)",
        description="Простое системное действие",
        guidance=(
            "Действуй прямо и без лишних шагов. Проверь фактический результат. "
            "Не пиши подробный технический отчёт, шаги выполнения или листинги PowerShell кода. "
            "Ответь предельно кратко 1 предложением по факту выполнения."
        ),
    )
