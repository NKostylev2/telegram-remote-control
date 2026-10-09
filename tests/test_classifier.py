"""Tests for automatic task classification and reasoning effort recommendation."""

from remote_control.agent.classifier import TaskProfile, classify_task


def test_classify_simple_actions() -> None:
    """Test that direct actions are classified as 'action' with 'low' effort."""
    prompts = [
        "запусти блокнот",
        "открой стим",
        "закрой браузер chrome",
        "убей процесс notepad",
        "start calc",
        "создай файл hello.txt",
    ]
    for prompt in prompts:
        profile = classify_task(prompt)
        assert isinstance(profile, TaskProfile)
        assert profile.task_type == "action"
        assert profile.effort == "low"
        assert "⚡" in profile.label
        assert "1 предложением" in profile.guidance


def test_classify_queries() -> None:
    """Test that queries and inspections are classified as 'query' with 'low' effort."""
    prompts = [
        "покажи список процессов",
        "какие файлы в папке",
        "выведи логи за последний час",
        "прочитай содержимое config.json",
        "сколько свободного места на диске C:",
        "найди все файлы .py",
    ]
    for prompt in prompts:
        profile = classify_task(prompt)
        assert isinstance(profile, TaskProfile)
        assert profile.task_type == "query"
        assert profile.effort == "low"
        assert "📋" in profile.label
        assert "таблицы или компактного списка" in profile.guidance


def test_classify_complex_tasks() -> None:
    """Test that analytical and coding tasks are classified as 'complex' with 'high' effort."""
    prompts = [
        "проанализируй почему падает сервис",
        "исправь ошибку в обработчике",
        "найди баг в функции валидации",
        "сделай рефакторинг архитектуры модуля",
        "напиши тесты для нового контроллера",
        "security audit of this repository",
    ]
    for prompt in prompts:
        profile = classify_task(prompt)
        assert isinstance(profile, TaskProfile)
        assert profile.task_type == "complex"
        assert profile.effort == "high"
        assert "🧠" in profile.label
        assert "Тщательно проанализируй" in profile.guidance


def test_fallback_defaults_to_action() -> None:
    """Test that arbitrary simple instructions default to 'action' with 'low' effort."""
    profile = classify_task("hello world")
    assert profile.task_type == "action"
    assert profile.effort == "low"
