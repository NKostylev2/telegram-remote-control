# 🚀 Быстрый старт: Автономная работа контроллера в Termux

Настройка автоматического запуска и фоновой работы Telegram Remote Control Controller на Android.

---

## Предварительные требования

1. **Установите Termux и Termux:Boot из F-Droid** (не из Google Play):
   - [Termux](https://f-droid.org/packages/com.termux/)
   - [Termux:Boot](https://f-droid.org/packages/com.termux.boot/)

2. **Исключите из оптимизации батареи**:
   - Настройки Android → Приложения → Termux → Батарея → **Не оптимизировать**
   - Повторите для Termux:Boot

3. **Запустите Termux:Boot хотя бы один раз** (обязательно для регистрации)

---

## Автоматическая установка (рекомендуется)

Запустите установочный скрипт из корня проекта:

```bash
bash scripts/termux/setup-autostart.sh
```

Скрипт выполнит:
- ✅ Проверку окружения (venv, .env, пакеты)
- ✅ Установку скрипта управления `~/control-bot.sh`
- ✅ Настройку автозапуска через Termux:Boot
- ✅ Создание удобных псевдонимов команд

После установки примените изменения:

```bash
source ~/.bashrc
```

---

## Управление контроллером

### Запуск
```bash
bot-start
```

### Проверка статуса
```bash
bot-status
```

### Просмотр логов
```bash
bot-logs        # Последние 50 строк
bot-tail        # В реальном времени
```

### Остановка
```bash
bot-stop
```

### Перезапуск
```bash
bot-restart
```

---

## Проверка автозапуска

1. Перезагрузите телефон
2. Подождите 2-3 минуты
3. Проверьте статус:
   ```bash
   bot-status
   ```
4. Или отправьте `/status` боту в Telegram

---

## Ручная установка (альтернатива)

Если предпочитаете ручную настройку:

1. **Скопируйте скрипты**:
   ```bash
   cp scripts/termux/control-bot.sh ~/control-bot.sh
   chmod +x ~/control-bot.sh
   
   mkdir -p ~/.termux/boot
   cp scripts/termux/boot-start-controller.sh ~/.termux/boot/start-controller.sh
   chmod +x ~/.termux/boot/start-controller.sh
   ```

2. **Добавьте псевдонимы в ~/.bashrc**:
   ```bash
   cat >> ~/.bashrc << 'EOF'
   
   # Telegram Remote Control aliases
   alias bot-start='~/control-bot.sh start'
   alias bot-stop='~/control-bot.sh stop'
   alias bot-restart='~/control-bot.sh restart'
   alias bot-status='~/control-bot.sh status'
   alias bot-logs='~/control-bot.sh logs'
   alias bot-tail='~/control-bot.sh tail'
   EOF
   ```

3. **Примените изменения**:
   ```bash
   source ~/.bashrc
   ```

---

## Устранение проблем

### Контроллер не запускается автоматически

1. Проверьте логи автозагрузки:
   ```bash
   cat ~/boot.log
   ```

2. Убедитесь, что Termux:Boot запускался хотя бы раз

3. Проверьте права на выполнение:
   ```bash
   ls -la ~/.termux/boot/start-controller.sh
   ```

### Контроллер останавливается через несколько минут

1. Проверьте настройки батареи — Android может игнорировать исключения
2. Отключите "адаптивную батарею" в настройках (если есть)
3. Убедитесь, что wake-lock активен:
   ```bash
   termux-wake-lock
   ```

### Процесс запущен, но бот не отвечает

1. Проверьте логи:
   ```bash
   bot-logs
   ```

2. Проверьте токен бота в `.env`:
   ```bash
   grep TELEGRAM_BOT_TOKEN .env
   ```

3. Проверьте сеть:
   ```bash
   ping -c 3 api.telegram.org
   ```

---

## 📚 Дополнительная документация

- [docs/TERMUX_AUTOSTART.md](docs/TERMUX_AUTOSTART.md) — Полное руководство по автозапуску
- [README.md](README.md) — Общая документация проекта
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — Архитектура системы

---

## ✅ Контрольный список

- [ ] Termux:Boot установлен из F-Droid
- [ ] Termux:Boot запускался хотя бы один раз
- [ ] Termux исключён из оптимизации батареи
- [ ] Termux:Boot исключён из оптимизации батареи
- [ ] Запущен `bash scripts/termux/setup-autostart.sh`
- [ ] Примены псевдонимы: `source ~/.bashrc`
- [ ] Контроллер запущен: `bot-start`
- [ ] Статус проверен: `bot-status`
- [ ] Телефон перезагружен для проверки автозапуска
- [ ] Бот отвечает в Telegram после перезагрузки
