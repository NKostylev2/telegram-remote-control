# Telegram Remote Control v2 (Android Controller + Windows Agent)

Система защищённого удалённого управления компьютером под управлением ОС Windows через Telegram-бота. Позволяет дистанционно включать выключенный ПК по локальной сети с помощью сетевых пакетов **Wake-on-LAN (WoL)**, ставить задачи в долговременную очередь, исполнять их в **Antigravity CLI (`agy`)**, получать снимки экрана со всех мониторов и отслеживать статус ПК.

> [!IMPORTANT]
> **Текущий статус проекта:** Завершена реализация распределённой архитектуры **Версии 2.0 (Android Controller + Windows Agent)**.
> - **Android Controller** работает 24/7 в Termux на смартфоне, единолично обрабатывает 100% запросов Telegram Bot API, хранит очередь в SQLite WAL (`data/tasks.sqlite3`), отправляет WoL Magic Packet и предоставляет внутренний защищённый HTTP API (`aiohttp.web`).
> - **Windows Agent** запускается на рабочем компьютере, никогда не опрашивает Telegram Bot API напрямую, с высокой частотой (250–500 мс) опрашивает очередь Контроллера, отправляет heartbeats, выполняет задачи через `AgyClient` (`agy.exe`), делает скриншоты и обрабатывает отмену задач.
> - **Протокол v2.0**: взаимная криптографическая аутентификация `HMAC-SHA256` (`X-Agent-Id`, `X-Timestamp`, `X-Signature`) с защитой от Replay-атак по временной метке.
> - Полный набор автоматических тестов: **265 тестов успешно пройдены** (`265 passed`), линтер `ruff` чист (0 ошибок).

---

## Архитектура и сетевая модель

Для пользователя существует **ровно один Telegram-бот, один токен и единый набор команд**. Разделение на Контроллер и Агент прозрачно для пользователя:

```
[Пользователь Telegram]
          │
          │ (HTTPS Polling к api.telegram.org)
          ▼
┌────────────────────────────────────────────────────────┐
│             Android Controller (Termux)                │
│                                                        │
│  • aiogram 3.x Telegram Bot (100% владение polling)   │
│  • Авторизация пользователей (ALLOWED_TELEGRAM_USERS)  │
│  • TaskStore & Очередь (SQLite WAL data/tasks.sqlite3) │
│  • Wake-on-LAN UDP:9 Broadcast модуль                  │
│  • aiohttp HTTP Server (/api/v1/*, порт 8080)          │
│  • AgentDispatcher (учёт heartbeat, online/offline)   │
│  • RemoteTaskExecutor (передача задач агенту)          │
└────────────────────────────────────────────────────────┘
          ▲                                ▲
          │ (WoL UDP:9 Broadcast)          │ (Внутренний HTTP API v2.0)
          │                                │ (HMAC-SHA256, 250-500 мс poll)
          ▼                                ▼
┌────────────────────────────────────────────────────────┐
│                   Целевой Windows-ПК                   │
│                                                        │
│  [Сетевой адаптер (NIC)] ──(Boot)──> [Windows Agent]   │
│                                              │         │
│  • AgentHttpClient (HMAC подпись)            │         │
│  • AgentRunner (фоновый цикл 250-500 мс)     │         │
│  • Отправка Heartbeat (статус, CLI, диски)   │         │
│  • AgyClient (прямой запуск agy.exe) ────────▼         │
│  • Модуль скриншотов (ImageGrab)      [agy.exe CLI]    │
│  • Graceful shutdown & taskkill /T /F                  │
└────────────────────────────────────────────────────────┘
```

### Принципы взаимодействия:
1. **Монополия на Telegram**: Telegram Bot API опрашивает исключительно Android Controller. Windows Agent никогда не запускает собственный polling и не конфликтует с серверами Telegram.
2. **Независимость от CLI на мобильном устройстве**: Контроллер не требует установки `agy.exe` и исполняется в легковесной среде Python.
3. **Безопасность периметра**: Взаимодействие Контроллера и Агента происходит внутри домашней Wi-Fi/LAN сети или закрытого VPN-туннеля (Tailscale/WireGuard). Проброс портов во внешний Интернет не требуется.
4. **Реактивная пауза очереди**: Если ПК выключен, задачи `/run` безопасно ставятся в очередь SQLite. При включении ПК и первом heartbeat Агента очередь мгновенно просыпается (`queue.notify()`) и передаёт задачи на исполнение без задержек.

---

## Команды Telegram-бота

| Команда | Описание | Поведение в распределённом режиме (v2) |
| :--- | :--- | :--- |
| `/start` | Приветствие и быстрый старт | Список доступных команд |
| `/help` | Подробная справка | Разъяснение параметров и режимов работы |
| `/status` | Диагностика системы | Отображает статус Контроллера, статус Windows Agent (Online/Offline, хост, ОС, CLI), размер очереди |
| `/workspace` | Список рабочих директорий | Запрашивает актуальный список доступных директорий с Windows Agent; скрывает абсолютные пути |
| `/run` | Интерактивный запуск задачи | Быстрый выбор диска и папки, выбор уровня рассуждений (`--effort low\|medium\|high`), постановка в очередь (даже при выключенном ПК) |
| `/tasks` | Очередь и история задач | Просмотр последних 10 задач текущего пользователя со статусами (`В очереди`, `Назначена`, `Выполняется`, `Успешно`, `Отменена`) |
| `/screenshot` | Снимок экрана Windows | Запрашивает скриншот у Windows Agent через HTTP API и отправляет в чат (фото + fallback-документ) |
| `/cancel <id>` | Принудительная отмена задачи | Отменяет задачу в очереди либо немедленно завершает дерево процессов `agy.exe` на Windows через `taskkill /T /F` |
| `/result <id>` | Получение результата задачи | Текстовый ответ или выгрузка полного файла результатов `task_<id>_output.txt` в виде документа |
| `/wake` | Включение ПК (Wake-on-LAN) | Отправка 3x Magic Packet (UDP:9) и ожидание подключения Windows Agent с наглядным обновлением статуса |

---

## Быстрый старт и развёртывание

Поддерживаются два режима работы:
- **Distributed Mode** (`CONTROLLER_MODE=distributed`) — основной боевой режим (Android Controller + Windows Agent).
- **Local Mode** (`CONTROLLER_MODE=local`) — монолитный режим для локальной разработки и отладки на одном компьютере.

---

### Вариант 1: Развёртывание в Distributed Mode (Production)

#### Шаг 1. Настройка Android Controller (Termux)

1. **Установка Termux**:
   - Установите [Termux](https://f-droid.org/packages/com.termux/) и [Termux:Boot](https://f-droid.org/packages/com.termux.boot/) из F-Droid.
   - В настройках Android отключите оптимизацию батареи для Termux («Не экономить заряд» / «Без ограничений»).
   - Разрешите автозапуск приложения в фоне.

2. **Установка пакетов в Termux**:
   ```bash
   pkg update && pkg upgrade -y
   pkg install python git -y
   ```

3. **Клонирование репозитория и установка**:
   ```bash
   git clone https://github.com/NKostylev2/telegram-remote-control.git
   cd telegram-remote-control
   git checkout feature/android-controller-windows-agent

   python -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -e .
   ```

4. **Настройка конфигурации `.env` на Android**:
   ```bash
   cp .env.example .env
   nano .env
   ```
   Укажите следующие параметры:
   ```env
   # Режим контроллера
   CONTROLLER_MODE=distributed
   CONTROLLER_HOST=0.0.0.0
   CONTROLLER_PORT=8080

   # Секретный ключ взаимной аутентификации (минимум 16 символов, одинаковый с агентом!)
   AGENT_AUTH_SECRET=SUPER_SECURE_RANDOM_SECRET_KEY_12345

   # Telegram
   TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
   ALLOWED_TELEGRAM_USERS=123456789

   # Настройки Wake-on-LAN для пробуждения ПК
   TARGET_MAC_ADDRESS=AA:BB:CC:DD:EE:FF
   WOL_BROADCAST_IP=192.168.1.255
   WOL_PORT=9

   # Предопределённые псевдонимы (для навигации в Telegram)
   ALLOWED_WORKSPACES={"backend": "M:\\dev\\my_project", "docs": "M:\\dev\\docs"}
   ```

5. **Запуск Android Controller**:
   Для предотвращения засыпания Termux активируйте wake-lock и запустите контроллер:
   ```bash
   termux-wake-lock
   remote-control-bot
   ```
   Либо в фоновом режиме через `nohup`:
   ```bash
   nohup remote-control-bot > controller.log 2>&1 &
   ```

6. **(Опционально) Автозапуск при перезагрузке смартфона через Termux:Boot**:
   Создайте файл `~/.termux/boot/start-controller.sh`:
   ```bash
   mkdir -p ~/.termux/boot
   cat << 'EOF' > ~/.termux/boot/start-controller.sh
   #!/data/data/com.termux/files/usr/bin/sh
   termux-wake-lock
   cd ~/telegram-remote-control
   source .venv/bin/activate
   nohup remote-control-bot > controller.log 2>&1 &
   EOF
   chmod +x ~/.termux/boot/start-controller.sh
   ```

---

#### Шаг 2. Настройка Windows Agent (Целевой ПК)

1. **Требования**:
   - Windows 10/11.
   - Python 3.11+.
   - Установленный и авторизованный Antigravity CLI (`agy.exe`).

2. **Установка зависимостей на Windows**:
   ```powershell
   cd "M:\dev\Telegram Remote Control"
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -e .
   ```

3. **Настройка конфигурации `.env` на Windows**:
   Создайте/отредактируйте `.env`:
   ```env
   # Режим распределённого агента
   CONTROLLER_MODE=distributed

   # IP-адрес смартфона с Android Controller в локальной сети (или VPN)
   CONTROLLER_HOST=192.168.1.50
   CONTROLLER_PORT=8080

   # Тот же секретный ключ, что и на Android!
   AGENT_AUTH_SECRET=SUPER_SECURE_RANDOM_SECRET_KEY_12345

   # Разрешённые рабочие каталоги на Windows
   ALLOWED_WORKSPACES={"backend": "M:\\dev\\my_project", "docs": "M:\\dev\\docs"}
   ANTIGRAVITY_CLI_PATH=agy
   ```

4. **Запуск Windows Agent**:
   ```powershell
   remote-control-agent
   ```
   Или:
   ```powershell
   python -m remote_control.agent.main
   ```

5. **(Опционально) Автозапуск Windows Agent при входе в систему**:
   Создайте задачу в Windows Task Scheduler:
   - Действие: Запуск программы.
   - Программа/скрипт: `M:\dev\Telegram Remote Control\.venv\Scripts\remote-control-agent.exe`.
   - Рабочая папка: `M:\dev\Telegram Remote Control`.
   - Триггер: При входе любого пользователя в систему.

---

### Вариант 2: Развёртывание в Local Mode (Разработка на одном ПК)

Для отладки без использования Android-устройства:
1. В `.env` укажите:
   ```env
   CONTROLLER_MODE=local
   TELEGRAM_BOT_TOKEN=...
   ALLOWED_TELEGRAM_USERS=...
   ALLOWED_WORKSPACES={"backend": "M:\\dev\\my_project"}
   ANTIGRAVITY_CLI_PATH=agy
   ```
2. Запустите бота:
   ```powershell
   remote-control-bot
   ```

---

## Тестирование и верификация

Все тесты полностью автономны и не требуют внешнего сетевого подключения, реального бота или `agy.exe`:

```powershell
# Запуск полного набора автоматических тестов (265 тестов)
python -m pytest -v

# Проверка качества и форматирования кода через Ruff
ruff check .
```

---

## Навигация по проектной документации

* [GEMINI.md](GEMINI.md) — Основной свод правил, стандартов кодирования и ограничений разработки.
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — Детальное описание архитектуры v2, протокол взаимодействия HTTP/HMAC, физика WoL и модель состояний задач.
* [docs/PROJECT_SPEC.md](docs/PROJECT_SPEC.md) — Спецификация функциональных требований и пользовательских сценариев.
* [docs/SECURITY.md](docs/SECURITY.md) — Модель угроз, криптографическая валидация HMAC-SHA256, защита от Replay-атак и изоляция рабочих процессов.
* [docs/ROADMAP.md](docs/ROADMAP.md) — Поэтапный отчет о завершённых стадиях разработки и критериях приёмки.
