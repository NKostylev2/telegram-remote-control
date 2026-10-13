#!/data/data/com.termux/files/usr/bin/bash
# Управление контроллером Telegram Remote Control в Termux
# Использование: ./control-bot.sh {start|stop|restart|status|logs}

PIDFILE="$HOME/.termux/controller.pid"
LOGFILE="$HOME/controller.log"
BOOT_LOG="$HOME/boot.log"

# Поиск директории проекта
find_project_dir() {
    if [ -f "$HOME/.termux/project_dir" ]; then
        local saved_dir
        saved_dir="$(cat "$HOME/.termux/project_dir" 2>/dev/null)"
        if [ -n "$saved_dir" ] && [ -f "$saved_dir/pyproject.toml" ]; then
            echo "$saved_dir"
            return 0
        fi
    fi
    for dir in "$HOME/Telegram-Remote-Control" "$HOME/telegram-remote-control" "$HOME/projects/Telegram-Remote-Control"; do
        if [ -f "$dir/pyproject.toml" ]; then
            echo "$dir"
            return 0
        fi
    done
    return 1
}

PROJECT_DIR=$(find_project_dir)
if [ -z "$PROJECT_DIR" ]; then
    echo "❌ Директория проекта не найдена"
    exit 1
fi

start_controller() {
    if [ -f "$PIDFILE" ]; then
        local existing_pid
        existing_pid=$(cat "$PIDFILE" 2>/dev/null)
        if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
            echo "❌ Контроллер уже запущен (PID: $existing_pid)"
            return 1
        fi
    fi

    echo "▶️  Запуск контроллера..."
    cd "$PROJECT_DIR" || { echo "❌ Не удалось перейти в $PROJECT_DIR"; exit 1; }

    if [ ! -f .venv/bin/activate ]; then
        echo "❌ Виртуальное окружение не найдено в $PROJECT_DIR/.venv"
        exit 1
    fi

    source .venv/bin/activate || { echo "❌ Не удалось активировать venv"; exit 1; }

    if [ ! -f .env ]; then
        echo "❌ Файл .env не найден в $PROJECT_DIR"
        exit 1
    fi

    nohup python -m remote_control.controller.main >> "$LOGFILE" 2>&1 &
    CONTROLLER_PID=$!
    echo $CONTROLLER_PID > "$PIDFILE"

    if command -v termux-wake-lock >/dev/null 2>&1; then
        termux-wake-lock
    fi

    sleep 1

    if kill -0 "$CONTROLLER_PID" 2>/dev/null; then
        echo "✅ Контроллер запущен (PID: $CONTROLLER_PID)"
        echo "📝 Логи: tail -f $LOGFILE"
    else
        echo "❌ Не удалось запустить контроллер, проверьте логи:"
        tail -n 20 "$LOGFILE"
        rm -f "$PIDFILE"
        exit 1
    fi
}

stop_controller() {
    if [ ! -f "$PIDFILE" ]; then
        echo "❌ Контроллер не запущен (нет PID-файла)"
        return 1
    fi

    PID=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo "⏹️  Остановка контроллера (PID: $PID)..."
        kill -TERM "$PID"

        # Ожидание корректного завершения (до 10 секунд)
        for i in {1..10}; do
            if ! kill -0 "$PID" 2>/dev/null; then
                break
            fi
            sleep 1
        done

        # Принудительное завершение если не остановился
        if kill -0 "$PID" 2>/dev/null; then
            echo "⚠️  Принудительное завершение..."
            kill -KILL "$PID"
            sleep 1
        fi

        rm -f "$PIDFILE"

        if command -v termux-wake-unlock >/dev/null 2>&1; then
            termux-wake-unlock
        fi

        echo "✅ Контроллер остановлен"
    else
        echo "❌ Процесс не найден, удаляю устаревший PID"
        rm -f "$PIDFILE"
    fi
}

status_controller() {
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE" 2>/dev/null)
        if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
            echo "✅ Контроллер работает (PID: $PID)"
            echo ""
            echo "Информация о процессе:"
            ps -p "$PID" -o pid,comm,%cpu,%mem,etime 2>/dev/null || \
                ps | grep "$PID" | head -1
            echo ""
            echo "📁 Директория проекта: $PROJECT_DIR"
            echo "📝 Лог-файл: $LOGFILE"
            echo ""
            echo "Последние 5 строк лога:"
            tail -n 5 "$LOGFILE" 2>/dev/null || echo "(лог пуст)"
            return 0
        else
            echo "❌ Контроллер не работает (устаревший PID: $PID)"
            rm -f "$PIDFILE"
            return 1
        fi
    else
        echo "❌ Контроллер не запущен"
        echo ""
        echo "Для запуска используйте: $0 start"
        return 1
    fi
}

view_logs() {
    local lines="${2:-50}"

    echo "=== Логи контроллера (последние $lines строк) ==="
    if [ -f "$LOGFILE" ]; then
        tail -n "$lines" "$LOGFILE"
    else
        echo "❌ Лог-файл не найден: $LOGFILE"
    fi

    echo ""
    echo "=== Логи автозагрузки (последние 10 строк) ==="
    if [ -f "$BOOT_LOG" ]; then
        tail -n 10 "$BOOT_LOG"
    else
        echo "(нет логов автозагрузки)"
    fi
}

follow_logs() {
    if [ -f "$LOGFILE" ]; then
        echo "📝 Просмотр логов в реальном времени (Ctrl+C для выхода)"
        tail -f "$LOGFILE"
    else
        echo "❌ Лог-файл не найден"
        exit 1
    fi
}

case "$1" in
    start)
        start_controller
        ;;
    stop)
        stop_controller
        ;;
    restart)
        echo "🔄 Перезапуск контроллера..."
        stop_controller
        sleep 2
        start_controller
        ;;
    status)
        status_controller
        ;;
    logs)
        view_logs "$@"
        ;;
    tail|follow)
        follow_logs
        ;;
    *)
        echo "Telegram Remote Control - Управление контроллером"
        echo ""
        echo "Использование: $0 {команда}"
        echo ""
        echo "Команды:"
        echo "  start      Запустить контроллер"
        echo "  stop       Остановить контроллер"
        echo "  restart    Перезапустить контроллер"
        echo "  status     Показать статус контроллера"
        echo "  logs       Показать последние логи (по умолчанию 50 строк)"
        echo "  tail       Следить за логами в реальном времени"
        echo ""
        exit 1
        ;;
esac
