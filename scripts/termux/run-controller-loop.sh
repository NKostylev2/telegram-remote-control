#!/data/data/com.termux/files/usr/bin/bash
# Supervisor контроллера Telegram Remote Control для Termux.
#
# Перезапускает контроллер при любом неожиданном завершении (падение, kill, занятый порт),
# с нарастающей паузой MIN -> MAX секунд; пауза сбрасывается, если контроллер проработал
# достаточно долго. Лог ротируется по размеру при каждом (пере)запуске.
# Остановка: SIGTERM/SIGINT этому процессу (останавливает и контроллер).
#
# Использование: run-controller-loop.sh <каталог_проекта>
#
# Переменные окружения (для тонкой настройки и тестов):
#   CONTROLLER_LOG             файл лога            (по умолчанию ~/controller.log)
#   SUPERVISOR_MIN_BACKOFF     первая пауза, с      (5)
#   SUPERVISOR_MAX_BACKOFF     максимальная пауза   (60)
#   SUPERVISOR_HEALTHY_SECONDS "здоровая" работа, после которой пауза сбрасывается (120)
#   SUPERVISOR_MAX_LOG_BYTES   порог ротации лога   (5242880)
#   SUPERVISOR_PYTHON          интерпретатор        (python)

set -u

PROJECT_DIR="${1:-}"
if [ -z "$PROJECT_DIR" ] || [ ! -f "$PROJECT_DIR/pyproject.toml" ]; then
    echo "Использование: $0 <каталог_проекта> (pyproject.toml не найден)" >&2
    exit 2
fi

LOGFILE="${CONTROLLER_LOG:-$HOME/controller.log}"
MIN_BACKOFF="${SUPERVISOR_MIN_BACKOFF:-5}"
MAX_BACKOFF="${SUPERVISOR_MAX_BACKOFF:-60}"
HEALTHY_SECONDS="${SUPERVISOR_HEALTHY_SECONDS:-120}"
MAX_LOG_BYTES="${SUPERVISOR_MAX_LOG_BYTES:-5242880}"
PYTHON_BIN="${SUPERVISOR_PYTHON:-python}"

CHILD_PID=""
SLEEP_PID=""
STOPPING=0

log() {
    printf '[%s] [supervisor] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOGFILE"
}

rotate_log() {
    local size
    size=$(wc -c < "$LOGFILE" 2>/dev/null || echo 0)
    if [ "${size:-0}" -gt "$MAX_LOG_BYTES" ]; then
        mv -f "$LOGFILE" "$LOGFILE.1"
        log "лог превысил ${MAX_LOG_BYTES} байт, предыдущий сохранён в $LOGFILE.1"
    fi
}

on_stop() {
    STOPPING=1
    if [ -n "$SLEEP_PID" ]; then
        kill "$SLEEP_PID" 2>/dev/null
    fi
    if [ -n "$CHILD_PID" ]; then
        kill -TERM "$CHILD_PID" 2>/dev/null
    fi
}
trap on_stop TERM INT

cd "$PROJECT_DIR" || exit 1
if [ ! -f .venv/bin/activate ]; then
    log "ОШИБКА: виртуальное окружение не найдено в $PROJECT_DIR/.venv"
    exit 1
fi
# shellcheck disable=SC1091
. .venv/bin/activate

log "supervisor запущен (PID $$), проект: $PROJECT_DIR"
backoff="$MIN_BACKOFF"

while [ "$STOPPING" -eq 0 ]; do
    rotate_log
    started=$(date +%s)

    "$PYTHON_BIN" -m remote_control.controller.main >> "$LOGFILE" 2>&1 &
    CHILD_PID=$!
    log "контроллер запущен (PID $CHILD_PID)"

    wait "$CHILD_PID"
    code=$?

    if [ "$STOPPING" -eq 1 ]; then
        # wait прерван сигналом: дожидаемся реального завершения контроллера
        wait "$CHILD_PID" 2>/dev/null
        log "остановлен по запросу"
        break
    fi

    runtime=$(( $(date +%s) - started ))
    log "контроллер завершился с кодом $code через ${runtime} с"

    if [ "$runtime" -ge "$HEALTHY_SECONDS" ]; then
        backoff="$MIN_BACKOFF"
    fi
    log "перезапуск через ${backoff} с"
    sleep "$backoff" &
    SLEEP_PID=$!
    wait "$SLEEP_PID" 2>/dev/null
    SLEEP_PID=""

    backoff=$(( backoff * 2 ))
    if [ "$backoff" -gt "$MAX_BACKOFF" ]; then
        backoff="$MAX_BACKOFF"
    fi
done

exit 0
