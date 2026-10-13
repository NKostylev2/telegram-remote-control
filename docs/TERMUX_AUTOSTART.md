# Автономная работа контроллера в Termux (Android)

Руководство по настройке автоматического запуска и фонового выполнения Telegram Remote Control Controller в Termux на Android.

---

## 1. Проблема

Контроллер запускается вручную командой `remote-control-bot`, но:
- Останавливается при закрытии приложения Termux
- Не запускается автоматически после перезагрузки телефона
- Прерывается при разрыве SSH-сессии

---

## 2. Решение: Termux:Boot + Wake Lock + Background Mode

> [!TIP]
> **Рекомендуемый быстрый способ установки (в 1 команду):**
> В проекте уже подготовлен скрипт автоматической настройки:
> ```bash
> bash scripts/termux/setup-autostart.sh
> ```
> Он автоматически проверит окружение, скопирует скрипты, настроит псевдонимы команд и активирует автозапуск. Краткая шпаргалка доступна в [QUICKSTART_TERMUX.md](../QUICKSTART_TERMUX.md). Ниже приведено пошаговое описание ручной настройки и внутренней работы каждого компонента.

### 2.1. Установка необходимых компонентов

1. **Установите Termux:Boot из F-Droid** (не из Google Play):
   ```
   https://f-droid.org/packages/com.termux.boot/
   ```

2. **Установите Termux:API** (опционально, для расширенных возможностей):
   ```
   https://f-droid.org/packages/com.termux.api/
   ```

3. **Установите пакеты в Termux**:
   ```bash
   pkg install termux-services termux-api
   ```

### 2.2. Отключение оптимизации батареи

Android агрессивно останавливает фоновые процессы для экономии батареи. Необходимо исключить Termux:

1. Откройте **Настройки Android** → **Приложения** → **Termux**
2. Найдите **Батарея** или **Оптимизация батареи**
3. Выберите **Не оптимизировать** или **Неограниченно**
4. Повторите для **Termux:Boot**

### 2.3. Создание скрипта автозапуска

Создайте директорию для скриптов автозагрузки:

```bash
mkdir -p ~/.termux/boot
```

Создайте файл `~/.termux/boot/start-controller.sh`:

```bash
#!/data/data/com.termux/files/usr/bin/bash

# Логирование старта
echo "[$(date)] Starting Telegram Remote Control Controller..." >> ~/boot.log

# Ожидание полной инициализации системы
sleep 30

# Переход в директорию проекта
cd ~/Telegram-Remote-Control || {
    echo "[$(date)] ERROR: Project directory not found" >> ~/boot.log
    exit 1
}

# Активация виртуального окружения
source .venv/bin/activate || {
    echo "[$(date)] ERROR: Failed to activate virtualenv" >> ~/boot.log
    exit 1
}

# Проверка конфигурации
if [ ! -f .env ]; then
    echo "[$(date)] ERROR: .env file not found" >> ~/boot.log
    exit 1
fi

# Запуск контроллера в фоне с перенаправлением вывода
nohup python -m remote_control.controller.main \
    >> ~/controller.log 2>&1 &

# Сохранение PID
echo $! > ~/.termux/controller.pid

# Удержание устройства от сна
termux-wake-lock

echo "[$(date)] Controller started with PID $(cat ~/.termux/controller.pid)" >> ~/boot.log
```

Сделайте скрипт исполняемым:

```bash
chmod +x ~/.termux/boot/start-controller.sh
```

### 2.4. Создание скрипта управления

Создайте файл `~/control-bot.sh` для ручного управления:

```bash
#!/data/data/com.termux/files/usr/bin/bash

PIDFILE="$HOME/.termux/controller.pid"
LOGFILE="$HOME/controller.log"
PROJECT_DIR="$HOME/Telegram-Remote-Control"

start_controller() {
    if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
        echo "❌ Контроллер уже запущен (PID: $(cat $PIDFILE))"
        return 1
    fi

    echo "▶️  Запуск контроллера..."
    cd "$PROJECT_DIR" || { echo "❌ Директория проекта не найдена"; exit 1; }
    source .venv/bin/activate || { echo "❌ Не удалось активировать venv"; exit 1; }

    nohup python -m remote_control.controller.main >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    
    termux-wake-lock
    echo "✅ Контроллер запущен (PID: $(cat $PIDFILE))"
}

stop_controller() {
    if [ ! -f "$PIDFILE" ]; then
        echo "❌ Контроллер не запущен (нет PID-файла)"
        return 1
    fi

    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "⏹️  Остановка контроллера (PID: $PID)..."
        kill -TERM "$PID"
        sleep 2
        
        if kill -0 "$PID" 2>/dev/null; then
            echo "⚠️  Принудительное завершение..."
            kill -KILL "$PID"
        fi
        
        rm -f "$PIDFILE"
        termux-wake-unlock
        echo "✅ Контроллер остановлен"
    else
        echo "❌ Процесс не найден, удаляю устаревший PID"
        rm -f "$PIDFILE"
    fi
}

status_controller() {
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "✅ Контроллер работает (PID: $PID)"
            echo ""
            ps -p "$PID" -o pid,comm,%cpu,%mem,etime
            return 0
        else
            echo "❌ Контроллер не работает (устаревший PID: $PID)"
            rm -f "$PIDFILE"
            return 1
        fi
    else
        echo "❌ Контроллер не запущен"
        return 1
    fi
}

view_logs() {
    if [ -f "$LOGFILE" ]; then
        tail -n 50 "$LOGFILE"
    else
        echo "❌ Лог-файл не найден"
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
        stop_controller
        sleep 1
        start_controller
        ;;
    status)
        status_controller
        ;;
    logs)
        view_logs
        ;;
    *)
        echo "Использование: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
```

Сделайте скрипт исполняемым:

```bash
chmod +x ~/control-bot.sh
```

---

## 3. Активация автозапуска

### 3.1. Первый запуск Termux:Boot

1. Откройте приложение **Termux:Boot** один раз (это обязательно для регистрации)
2. Закройте его
3. Перезагрузите телефон

### 3.2. Проверка автозапуска

После перезагрузки телефона:

```bash
~/control-bot.sh status
```

Если контроллер не запустился, проверьте логи:

```bash
cat ~/boot.log
cat ~/controller.log
```

---

## 4. Ручное управление

### Запуск контроллера
```bash
~/control-bot.sh start
```

### Остановка контроллера
```bash
~/control-bot.sh stop
```

### Перезапуск контроллера
```bash
~/control-bot.sh restart
```

### Проверка статуса
```bash
~/control-bot.sh status
```

### Просмотр логов (последние 50 строк)
```bash
~/control-bot.sh logs
```

### Просмотр полных логов в реальном времени
```bash
tail -f ~/controller.log
```

---

## 5. Устранение проблем

### Контроллер не запускается после перезагрузки

1. **Проверьте права на выполнение**:
   ```bash
   ls -la ~/.termux/boot/start-controller.sh
   ```
   Должно быть: `-rwx------`

2. **Проверьте логи автозагрузки**:
   ```bash
   cat ~/boot.log
   ```

3. **Убедитесь, что Termux:Boot запускался хотя бы раз** после установки

4. **Проверьте настройки батареи** — Android может игнорировать исключения

### Контроллер останавливается через несколько минут

1. **Убедитесь, что wake-lock активен**:
   ```bash
   termux-wake-lock
   ```

2. **Проверьте настройки энергосбережения** для Termux и Termux:Boot

3. **Отключите "адаптивную батарею"** в настройках Android (если есть)

### Процесс запущен, но бот не отвечает в Telegram

1. **Проверьте логи контроллера**:
   ```bash
   ~/control-bot.sh logs
   ```

2. **Проверьте токен бота** в `.env`:
   ```bash
   grep TELEGRAM_BOT_TOKEN ~/Telegram-Remote-Control/.env
   ```

3. **Проверьте сетевое подключение**:
   ```bash
   ping -c 3 api.telegram.org
   ```

---

## 6. Мониторинг работы

### Создание псевдонима для быстрого доступа

Добавьте в `~/.bashrc`:

```bash
alias bot-start='~/control-bot.sh start'
alias bot-stop='~/control-bot.sh stop'
alias bot-restart='~/control-bot.sh restart'
alias bot-status='~/control-bot.sh status'
alias bot-logs='~/control-bot.sh logs'
alias bot-tail='tail -f ~/controller.log'
```

Примените изменения:

```bash
source ~/.bashrc
```

Теперь можно использовать короткие команды:
```bash
bot-status
bot-logs
bot-tail
```

---

## 7. Дополнительные рекомендации

### 7.1. Ротация логов

Создайте cron-задачу для очистки старых логов (если установлен `cronie`):

```bash
pkg install cronie
crontab -e
```

Добавьте:
```
0 3 * * 0 find ~/controller.log -size +10M -exec truncate -s 1M {} \;
```

### 7.2. Уведомления о запуске

Контроллер уже отправляет уведомление всем авторизованным пользователям при старте (см. `send_startup_notification` в `main.py`).

### 7.3. Запуск в tmux/screen для интерактивной отладки

Для длительных SSH-сессий с возможностью переподключения:

```bash
pkg install tmux
tmux new -s controller
cd ~/Telegram-Remote-Control
source .venv/bin/activate
python -m remote_control.controller.main
# Нажмите Ctrl+B, затем D для отсоединения
# Переподключение: tmux attach -t controller
```

---

## 8. Контрольный список настройки

- [ ] Установлен Termux:Boot из F-Droid
- [ ] Termux:Boot запускался хотя бы один раз
- [ ] Termux и Termux:Boot исключены из оптимизации батареи
- [ ] Создан скрипт `~/.termux/boot/start-controller.sh`
- [ ] Скрипт имеет права на выполнение (`chmod +x`)
- [ ] Создан скрипт управления `~/control-bot.sh`
- [ ] Файл `.env` содержит корректный `TELEGRAM_BOT_TOKEN`
- [ ] Телефон перезагружен для проверки автозапуска
- [ ] Контроллер отвечает в Telegram после перезагрузки

---

## 9. Альтернатива: systemd-style сервисы в Termux

Если `termux-services` установлен, можно использовать runit:

```bash
mkdir -p ~/.termux/sv/controller/log
```

Создайте `~/.termux/sv/controller/run`:

```bash
#!/data/data/com.termux/files/usr/bin/bash
cd ~/Telegram-Remote-Control
source .venv/bin/activate
exec python -m remote_control.controller.main 2>&1
```

Создайте `~/.termux/sv/controller/log/run`:

```bash
#!/data/data/com.termux/files/usr/bin/bash
mkdir -p ~/service-logs
exec svlogd -tt ~/service-logs/controller
```

Установите права:

```bash
chmod +x ~/.termux/sv/controller/run
chmod +x ~/.termux/sv/controller/log/run
```

Активируйте сервис:

```bash
sv-enable controller
sv up controller
```

Управление:
```bash
sv status controller    # Статус
sv down controller      # Остановить
sv up controller        # Запустить
sv restart controller   # Перезапустить
```

---

## 10. Проверка результата

После настройки перезагрузите телефон и через 2-3 минуты отправьте `/status` боту в Telegram.

Ожидаемый результат:
```
🤖 Telegram Remote Control перезапущен и готов к работе

⚙️ Режим: 🌐 Распределённый (Контроллер)
🕒 Время запуска: DD.MM.YYYY HH:MM:SS UTC

Отправьте задачу сообщением, воспользуйтесь меню или нажмите /status.
```
