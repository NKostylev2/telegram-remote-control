# Project Guidelines & Architecture for Claude (CLAUDE.md)

Файл инструкций и стандартов разработки проекта **Telegram Remote Control** для Claude Code и Claude Desktop.

---

## 1. О проекте (Project Overview)

**Telegram Remote Control** — защищённая распределённая система удалённого запуска и мониторинга задач Antigravity CLI (`agy.exe`) на рабочей Windows-машине через Telegram-бота с поддержкой Wake-on-LAN.

### Архитектура v2 (Distributed Mode):
1. **Controller (Android / Termux)**:
   - Долговременный узел, доступен круглосуточно.
   - Запускает Telegram-бота (`aiogram` 3.x, Long Polling).
   - Хранит очередь и историю в SQLite (`data/tasks.sqlite3`, режим WAL).
   - Поднимает внутренний HTTP API-сервер (`aiohttp.web`, порт по умолчанию 8080) для связи с Windows Agent.
   - Отправляет Wake-on-LAN Magic Packet для включения ПК.
2. **Windows Agent (Рабочий ПК)**:
   - Запускается на целевой машине Windows (`remote-control-agent`).
   - Подключается к Controller по HTTP API с аутентификацией **HMAC-SHA256** (`X-Agent-Id`, `X-Timestamp`, `X-Signature`, защита от Replay-атак).
   - Опрашивает очередь задач (concurrency = 1), запускает `agy.exe` через `AgyClient` (без shell-оболочки), перехватывает вывод и код возврата.
   - Делает скриншоты рабочего стола (`Pillow` / Win32 GDI) и передаёт Контроллеру.
   - Отправляет Heartbeat каждые 10 сек; поддерживает кооперативную отмену задач (`taskkill /T /F`).

---

## 2. Команды для разработки и проверки

В проекте настроено виртуальное окружение в каталоге `.venv` (Python 3.11):

- **Запуск тестов**:
  ```powershell
  .venv\Scripts\pytest.exe
  ```
  *(Все 286 тестов должны проходить успешно)*

- **Проверка линтером**:
  ```powershell
  .venv\Scripts\ruff.exe check .
  ```

- **Проверка форматирования**:
  ```powershell
  .venv\Scripts\ruff.exe format --check .
  ```

- **Запуск сервисов локально**:
  - Controller (Telegram Bot): `.venv\Scripts\python.exe -m remote_control.controller.main` (или `remote-control-bot`)
  - Windows Agent: `.venv\Scripts\python.exe -m remote_control.agent.main` (или `remote-control-agent`)

---

## 3. Фундаментальные правила и безопасность (Обязательно к исполнению)

1. **Никаких секретов в коде и репозитории**:
   - Токены бота, секреты HMAC (`AGENT_SECRET`), MAC-адреса и персональные ID запрещено хардкодить или логировать в открытом виде.
   - Для настроек используется `.env` (шаблон: `.env.example`).
2. **Запрет произвольного выполнения команд**:
   - Категорически запрещено добавлять интерфейсы для исполнения произвольных shell/PowerShell команд через Telegram.
   - Допустим только запуск задач через типизированный адаптер `AgyClient`.
3. **Белые списки (Allowlists)**:
   - Доступ к боту валидируется по `ALLOWED_TELEGRAM_USERS` (неавторизованные пользователи бесшумно игнорируются).
   - Рабочие директории валидируются по белому списку `ALLOWED_WORKSPACES`.
4. **Анализ перед реализацией**:
   - Перед изменениями сверяться с `docs/PROJECT_SPEC.md`, `docs/ARCHITECTURE.md` и `docs/SECURITY.md`.
5. **Малые шаги и обязательная верификация**:
   - Каждая задача завершается обязательным прогоном тестов (`pytest`) и линтера (`ruff`).
   - Нельзя завершать задачу без фактического подтверждения успешности тестов.
6. **Минимальный скоуп изменений**:
   - Не трогать файлы, функции и зависимости, не относящиеся к текущей задаче.

---

## 4. Структура кодовой базы

```text
src/remote_control/
├── config.py           # Pydantic-настройки (AppConfig)
├── common/             # Общие модули
│   ├── executor.py     # Исполнитель команд и утилиты
│   ├── models.py       # Модели данных и перечисления
│   ├── protocol.py     # Модели запросов/ответов HTTP API (v2) и HMAC-SHA256
│   └── wol.py          # Wake-on-LAN и проверка доступности хоста
├── controller/         # Узел Контроллера (Telegram Bot + Server)
│   ├── main.py         # Точка входа remote-control-bot
│   ├── bot.py          # Инициализация aiogram Bot и Dispatcher
│   ├── server.py       # HTTP сервер (API для Windows Agent)
│   ├── agent_dispatcher.py # AgentDispatcher (управление агентами)
│   ├── single_instance.py  # Файловая блокировка от дублирования процессов
│   ├── task_store.py   # SQLite хранилище задач (data/tasks.sqlite3)
│   ├── task_queue.py   # Асинхронная очередь задач (FIFO, concurrency=1)
│   ├── middlewares.py  # Авторизация по белому списку пользователей
│   └── handlers/       # Обработчики Telegram (/start, /run, /tasks, /wake и т.д.)
└── agent/              # Узел Windows Agent
    ├── main.py         # Точка входа remote-control-agent
    ├── runner.py       # Цикл опроса (polling), heartbeat, выполнение задач
    ├── client.py       # Клиент с HMAC-подписью к Controller
    ├── agy_client.py   # Адаптер вызова agy.exe и парсинг JSON-конверта
    ├── screenshot.py   # Захват скриншота рабочего стола (Pillow)
    └── classifier.py   # Классификация задач (--effort low|medium|high)

docs/
├── ARCHITECTURE.md     # Архитектура, сетевой протокол, структуры данных
├── PROJECT_SPEC.md     # Спецификация команд и требований
├── SECURITY.md         # Модель угроз и меры защиты
└── ROADMAP.md          # Дорожная карта и статус выполнения этапов
```

---

## 5. Текущий статус проекта

- Ветка: `feature/android-controller-windows-agent`
- Этапы 1–9 завершены:
  - Монолитный (`local`) и распределённый (`distributed`) режимы поддержаны.
  - Очередь SQLite с WAL и восстановлением при сбоях работает.
  - Wake-on-LAN протестирован и интегрирован в Telegram `/wake`.
  - Отмена задач с завершением дерева процессов (`taskkill /T /F`) работает.
  - Скриншоты по запросу работают.
  - Все 286 тестов проходят успешно.
