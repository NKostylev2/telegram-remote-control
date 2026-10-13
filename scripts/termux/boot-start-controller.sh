#!/data/data/com.termux/files/usr/bin/bash
# Автозапуск контроллера при загрузке Android (Termux:Boot)
# Установить в: ~/.termux/boot/start-controller.sh

# Логирование старта
echo "[$(date)] Starting Telegram Remote Control Controller..." >> ~/boot.log

# Удержание устройства от сна (активируем сразу, чтобы CPU не уснул во время ожидания)
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock
    echo "[$(date)] Wake lock acquired" >> ~/boot.log
else
    echo "[$(date)] WARNING: termux-wake-lock not found" >> ~/boot.log
fi

# Проверка, не запущен ли уже контроллер
PIDFILE="$HOME/.termux/controller.pid"
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[$(date)] Controller is already running with PID $OLD_PID" >> ~/boot.log
        exit 0
    fi
fi

# Ожидание полной инициализации системы и сети
sleep 30

# Определение директории проекта
PROJECT_DIR=""
if [ -f "$HOME/.termux/project_dir" ]; then
    SAVED_DIR=$(cat "$HOME/.termux/project_dir" 2>/dev/null)
    if [ -n "$SAVED_DIR" ] && [ -f "$SAVED_DIR/pyproject.toml" ]; then
        PROJECT_DIR="$SAVED_DIR"
    fi
fi

if [ -z "$PROJECT_DIR" ]; then
    for dir in "$HOME/Telegram-Remote-Control" "$HOME/telegram-remote-control" "$HOME/projects/Telegram-Remote-Control"; do
        if [ -f "$dir/pyproject.toml" ]; then
            PROJECT_DIR="$dir"
            break
        fi
    done
fi

if [ -z "$PROJECT_DIR" ]; then
    echo "[$(date)] ERROR: Project directory not found" >> ~/boot.log
    exit 1
fi

cd "$PROJECT_DIR" || {
    echo "[$(date)] ERROR: Cannot cd to project directory: $PROJECT_DIR" >> ~/boot.log
    exit 1
}

# Активация виртуального окружения
if [ ! -f .venv/bin/activate ]; then
    echo "[$(date)] ERROR: Virtualenv not found at $PROJECT_DIR/.venv" >> ~/boot.log
    exit 1
fi

source .venv/bin/activate || {
    echo "[$(date)] ERROR: Failed to activate virtualenv" >> ~/boot.log
    exit 1
}

# Проверка конфигурации
if [ ! -f .env ]; then
    echo "[$(date)] ERROR: .env file not found at $PROJECT_DIR/.env" >> ~/boot.log
    exit 1
fi

# Запуск контроллера в фоне с перенаправлением вывода
nohup python -m remote_control.controller.main \
    >> ~/controller.log 2>&1 &

# Сохранение PID
CONTROLLER_PID=$!
mkdir -p ~/.termux
echo $CONTROLLER_PID > "$PIDFILE"

# Проверка, что процесс успешно запустился
sleep 2
if kill -0 "$CONTROLLER_PID" 2>/dev/null; then
    echo "[$(date)] Controller started successfully with PID $CONTROLLER_PID" >> ~/boot.log
else
    echo "[$(date)] ERROR: Controller failed to start. Check ~/controller.log" >> ~/boot.log
    rm -f "$PIDFILE"
    exit 1
fi
