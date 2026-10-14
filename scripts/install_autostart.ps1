<#
.SYNOPSIS
    Установка или удаление автозапуска Telegram Remote Control при входе в Windows.
.DESCRIPTION
    Создаёт ярлык в папке автозагрузки текущего пользователя:
    %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
    Ярлык запускает start_services.bat в свернутом (Minimized) режиме.
.PARAMETER Uninstall
    Удалить ярлык автозапуска.
.PARAMETER Mode
    Режим запуска сервиса (auto, agent, controller, bot, both). По умолчанию "auto".
.PARAMETER Always
    Запускать службы при каждом входе в Windows, в том числе при ручном включении ПК.
    Без этого параметра службы стартуют только после запроса Wake-on-LAN от Контроллера (-RequireWol).
#>

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [ValidateSet("auto", "agent", "controller", "bot", "both")]
    [string]$Mode = "auto",

    [switch]$Always
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$BatPath = Join-Path $ProjectRoot "start_services.bat"
$StartupFolder = [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::Startup)
$ShortcutName = "Telegram Remote Control.lnk"
$ShortcutPath = Join-Path $StartupFolder $ShortcutName

if ($Uninstall) {
    if (Test-Path $ShortcutPath) {
        Remove-Item -Path $ShortcutPath -Force
        Write-Host "[OK] Ярлык автозапуска успешно удалён: '$ShortcutPath'" -ForegroundColor Green
    } else {
        Write-Host "[INFO] Ярлык автозапуска не найден в '$StartupFolder'." -ForegroundColor Yellow
    }
    exit 0
}

if (-not (Test-Path $BatPath)) {
    Write-Error "Файл запуска не найден: '$BatPath'"
    exit 1
}

try {
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
    # Без TargetPath ярлык «пустой» и при входе в Windows ничего не запускает.
    $Shortcut.TargetPath = $BatPath
    $shortcutArgs = @()
    if (-not $Always) {
        $shortcutArgs += "-RequireWol"
    }
    if ($Mode -ne "auto") {
        $shortcutArgs += "-Mode $Mode"
    }
    $Shortcut.Arguments = $shortcutArgs -join " "
    $Shortcut.WorkingDirectory = $ProjectRoot
    $Shortcut.Description = "Telegram Remote Control Service Launcher"
    $Shortcut.WindowStyle = 7 # 7 = Minimized (не мешает при входе в систему)
    $Shortcut.Save()

    # Проверяем результат: молчаливо «сломанный» ярлык — именно то, что мы чиним.
    $savedShortcut = $WshShell.CreateShortcut($ShortcutPath)
    if ($savedShortcut.TargetPath -ne $BatPath) {
        throw "Ярлык создан, но цель не сохранилась (TargetPath='$($savedShortcut.TargetPath)'). Автозапуск работать не будет."
    }

    Write-Host "[OK] Автозапуск успешно настроен!" -ForegroundColor Green
    Write-Host "  Ярлык: $ShortcutPath" -ForegroundColor Cyan
    Write-Host "  Цель:  $BatPath" -ForegroundColor Cyan
    Write-Host "  Папка: $ProjectRoot" -ForegroundColor Cyan
    Write-Host "  Режим: $Mode" -ForegroundColor Cyan
    Write-Host "  Аргументы: $($Shortcut.Arguments)" -ForegroundColor Cyan
    if ($Always) {
        Write-Host "  Запуск: при каждом входе в Windows (в т.ч. при ручном включении ПК)." -ForegroundColor Cyan
    } else {
        Write-Host "  Запуск: только после запроса Wake-on-LAN от Контроллера (для запуска всегда используйте -Always)." -ForegroundColor Cyan
    }
    Write-Host "`nСервисы (AG, Nova и Telegram Remote Control) будут автоматически стартовать при входе в Windows (в т.ч. после Wake-on-LAN)."
} catch {
    Write-Error "Не удалось создать ярлык автозапуска: $_"
    exit 1
}
