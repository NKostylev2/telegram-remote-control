#!/data/data/com.termux/files/usr/bin/bash
# Управление контроллером Telegram Remote Control в Termux
# Использование: ./control-bot.sh {start|stop|restart|status|logs}

PIDFILE="$HOME/.termux/controller.pid"
LOGFILE="$HOME/controller.log"
BOOT_LOG="$HOME/boot.log"

# Определяет, чем является процесс из PID-файла: supervisor | legacy | none.
# После перезагрузки Android старый PID из файла может принадлежать совсем другому
# процессу, поэтому одного `kill -0` недостаточно: сверяем командную строку.
#   supervisor - run-controller-loop.sh (текущая схема, перезапускает контроллер сам)
#   legacy     - контроллер, запущенный старой версией скрипта без supervisor
pid_kind() {
    local pid="$1" cmd
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        echo none
        return
    fi
    if [ -r "/proc/$pid/cmdline" ]; then
        cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
        case "$cmd" in
            *run-controller-loop*) echo supervisor ;;
            *remote_control.controller.main*) echo legacy ;;
            *) echo none ;;
        esac
    else
        echo supervisor
    fi
}

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
        local existing_pid kind
        existing_pid=$(cat "$PIDFILE" 2>/dev/null)
        kind=$(pid_kind "$existing_pid")
        case "$kind" in
            supervisor)
                echo "❌ Контроллер уже запущен (supervisor PID: $existing_pid)"
                return 1
                ;;
            legacy)
                echo "❌ Контроллер запущен старой версией скрипта без автоперезапуска (PID: $existing_pid)."
                echo "   Выполните: $0 restart"
                return 1
                ;;
        esac
        rm -f "$PIDFILE"
    fi

    echo "▶️  Запуск контроллера..."
    cd "$PROJECT_DIR" || { echo "❌ Не удалось перейти в $PROJECT_DIR"; exit 1; }

    if [ ! -f .venv/bin/activate ]; then
        echo "❌ Виртуальное окружение не найдено в $PROJECT_DIR/.venv"
        exit 1
    fi

    if [ ! -f .env ]; then
        echo "❌ Файл .env не найден в $PROJECT_DIR"
        exit 1
    fi

    local loop_script="$PROJECT_DIR/scripts/termux/run-controller-loop.sh"
    if [ ! -f "$loop_script" ]; then
        echo "❌ Не найден $loop_script. Обновите проект (git pull) и повторите."
        exit 1
    fi

    nohup bash "$loop_script" "$PROJECT_DIR" >> "$LOGFILE" 2>&1 &
    SUPERVISOR_PID=$!
    echo $SUPERVISOR_PID > "$PIDFILE"

    if command -v termux-wake-lock >/dev/null 2>&1; then
        termux-wake-lock
    fi

    sleep 1

    if kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
        echo "✅ Контроллер запущен под supervisor (PID: $SUPERVISOR_PID), при падении перезапустится сам"
        echo "📝 Логи: tail -f $LOGFILE"
    else
        echo "❌ Не удалось запустить supervisor, проверьте логи:"
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
    local kind
    kind=$(pid_kind "$PID")
    if [ "$kind" = "none" ]; then
        echo "❌ Процесс не найден (или PID занят чужим процессом), удаляю устаревший PID"
        rm -f "$PIDFILE"
        return 1
    fi

    echo "⏹️  Остановка контроллера (PID: $PID, $kind)..."
    kill -TERM "$PID"

    # Ожидание корректного завершения (до 10 секунд)
    for _ in {1..10}; do
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

    # Добиваем возможного «сироту»: контроллер, переживший принудительное завершение supervisor
    if command -v pkill >/dev/null 2>&1; then
        pkill -f "remote_control.controller.main" 2>/dev/null || true
    fi

    rm -f "$PIDFILE"

    if command -v termux-wake-unlock >/dev/null 2>&1; then
        termux-wake-unlock
    fi

    echo "✅ Контроллер остановлен"
}

status_controller() {
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE" 2>/dev/null)
        local kind
        kind=$(pid_kind "$PID")
        if [ "$kind" != "none" ]; then
            if [ "$kind" = "supervisor" ]; then
                echo "✅ Контроллер работает под supervisor (PID: $PID)"
            else
                echo "⚠️  Контроллер работает без supervisor, старая версия скрипта (PID: $PID)"
            fi
            echo ""
            echo "Информация о процессе:"
            ps -p "$PID" -o pid,comm,%cpu,%mem,etime 2>/dev/null || \
                ps | grep "$PID" | head -1
            if command -v pgrep >/dev/null 2>&1; then
                CHILD=$(pgrep -f "remote_control.controller.main" | head -1)
                if [ -n "$CHILD" ]; then
                    echo "Контроллер (python) PID: $CHILD"
                else
                    echo "⚠️  Процесс python контроллера сейчас не найден (идёт перезапуск?)"
                fi
            fi
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
