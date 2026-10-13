#!/data/data/com.termux/files/usr/bin/bash
# Автоматическая настройка автозапуска контроллера в Termux
# Использование: bash scripts/termux/setup-autostart.sh

set -e

echo "=================================================="
echo "   Настройка автозапуска контроллера в Termux"
echo "=================================================="
echo ""

# Определение директории проекта
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "📁 Директория проекта: $PROJECT_DIR"
echo ""

# Проверка необходимых файлов
echo "🔍 Проверка окружения..."

if [ ! -f "$PROJECT_DIR/pyproject.toml" ]; then
    echo "❌ ОШИБКА: pyproject.toml не найден"
    echo "   Убедитесь, что скрипт запускается из корня проекта"
    exit 1
fi

if [ ! -f "$PROJECT_DIR/.venv/bin/activate" ]; then
    echo "❌ ОШИБКА: Виртуальное окружение .venv не найдено"
    echo "   Создайте окружение: python -m venv .venv"
    exit 1
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "⚠️  ПРЕДУПРЕЖДЕНИЕ: Файл .env не найден"
    echo "   Скопируйте .env.example в .env и настройте токен бота"
    read -p "   Продолжить установку? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo "✅ Окружение проверено"
echo ""

# Установка необходимых пакетов
echo "📦 Проверка пакетов Termux..."

if ! command -v termux-wake-lock >/dev/null 2>&1; then
    echo "⚠️  termux-api не установлен"
    read -p "   Установить termux-api? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        pkg install -y termux-api
    fi
fi

echo ""

# Создание директорий
echo "📂 Создание необходимых директорий..."
mkdir -p ~/.termux/boot
mkdir -p ~/.termux
echo "$PROJECT_DIR" > ~/.termux/project_dir
echo "✅ Директории созданы"
echo ""

# Копирование скрипта управления
echo "📝 Установка скрипта управления..."
cp "$SCRIPT_DIR/control-bot.sh" ~/control-bot.sh
chmod +x ~/control-bot.sh
echo "✅ Скрипт управления установлен: ~/control-bot.sh"
echo ""

# Копирование скрипта автозагрузки
echo "📝 Установка скрипта автозагрузки..."
cp "$SCRIPT_DIR/boot-start-controller.sh" ~/.termux/boot/start-controller.sh
chmod +x ~/.termux/boot/start-controller.sh
echo "✅ Скрипт автозагрузки установлен: ~/.termux/boot/start-controller.sh"
echo ""

# Создание псевдонимов в bashrc
echo "⚙️  Настройка псевдонимов..."

if ! grep -q "# Telegram Remote Control aliases" ~/.bashrc 2>/dev/null; then
    cat >> ~/.bashrc << 'EOF'

# Telegram Remote Control aliases
alias bot-start='~/control-bot.sh start'
alias bot-stop='~/control-bot.sh stop'
alias bot-restart='~/control-bot.sh restart'
alias bot-status='~/control-bot.sh status'
alias bot-logs='~/control-bot.sh logs'
alias bot-tail='~/control-bot.sh tail'
EOF
    echo "✅ Псевдонимы добавлены в ~/.bashrc"
else
    echo "ℹ️  Псевдонимы уже установлены"
fi

echo ""

# Проверка установки Termux:Boot
echo "🔍 Проверка Termux:Boot..."
if [ -d "/data/data/com.termux.boot" ]; then
    echo "✅ Termux:Boot установлен"
else
    echo "⚠️  Termux:Boot НЕ УСТАНОВЛЕН"
    echo ""
    echo "   Для автозапуска при перезагрузке необходимо:"
    echo "   1. Установить Termux:Boot из F-Droid:"
    echo "      https://f-droid.org/packages/com.termux.boot/"
    echo "   2. Запустить приложение Termux:Boot один раз"
    echo "   3. Перезагрузить устройство"
    echo ""
fi

echo ""
echo "=================================================="
echo "   ✅ Установка завершена успешно!"
echo "=================================================="
echo ""
echo "📋 Следующие шаги:"
echo ""
echo "1. Исключите Termux из оптимизации батареи:"
echo "   Настройки → Приложения → Termux → Батарея → Не оптимизировать"
echo ""
echo "2. Если Termux:Boot не установлен, установите его:"
echo "   https://f-droid.org/packages/com.termux.boot/"
echo ""
echo "3. Примените псевдонимы:"
echo "   source ~/.bashrc"
echo ""
echo "4. Запустите контроллер:"
echo "   bot-start"
echo "   или: ~/control-bot.sh start"
echo ""
echo "5. Проверьте статус:"
echo "   bot-status"
echo ""
echo "6. Перезагрузите телефон для проверки автозапуска"
echo ""
echo "=================================================="
echo ""
echo "📝 Доступные команды:"
echo "   bot-start      Запустить контроллер"
echo "   bot-stop       Остановить контроллер"
echo "   bot-restart    Перезапустить контроллер"
echo "   bot-status     Показать статус"
echo "   bot-logs       Показать логи"
echo "   bot-tail       Следить за логами"
echo ""
echo "📚 Полная документация: docs/TERMUX_AUTOSTART.md"
echo ""
