<#
.SYNOPSIS
    Диагностика связки Windows Agent -> Android Controller: «почему ничего не работает после включения ПК».
.DESCRIPTION
    Проверяет по пунктам и показывает, где именно обрыв:
      1. ярлык автозапуска (существует ли, есть ли у него цель);
      2. процессы Remote Control на этом ПК (запущен ли агент, нет ли второго бота с тем же токеном);
      3. доступность Контроллера: GET /api/v1/status и признак agent_connected;
      4. хвосты логов агента и скрипта запуска.
    Код возврата: 0 — агент подключён к Контроллеру, 1 — есть проблемы.
.PARAMETER TailLines
    Сколько последних строк каждого лога показать (по умолчанию 15).
#>

[CmdletBinding()]
param(
    [int]$TailLines = 15
)

$ErrorActionPreference = "Continue"

$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$EnvFile = Join-Path $ProjectRoot ".env"
$LocalRoot = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $ProjectRoot }
$LogDir = Join-Path $LocalRoot "RemoteControl\logs"

$script:Problems = 0

function Show([string]$Level, [string]$Text) {
    $color = switch ($Level) { "OK" { "Green" } "WARN" { "Yellow" } "FAIL" { "Red" } default { "Gray" } }
    if ($Level -eq "FAIL") { $script:Problems++ }
    Write-Host ("[{0,-4}] {1}" -f $Level, $Text) -ForegroundColor $color
}

function Get-EnvValue([string]$Name, [string]$Default) {
    if (Test-Path $EnvFile) {
        foreach ($line in (Get-Content $EnvFile)) {
            if ($line -match "^\s*$Name\s*=\s*(.*?)\s*$") { return $matches[1] }
        }
    }
    return $Default
}

$controllerHost = Get-EnvValue "CONTROLLER_HOST" "127.0.0.1"
$controllerPort = Get-EnvValue "CONTROLLER_PORT" "8080"
$controllerMode = Get-EnvValue "CONTROLLER_MODE" "local"

Write-Host "=== Диагностика Telegram Remote Control ===" -ForegroundColor Cyan
Write-Host "Проект: $ProjectRoot"
Write-Host "Из .env: CONTROLLER_MODE=$controllerMode, CONTROLLER_HOST=$controllerHost, CONTROLLER_PORT=$controllerPort"
Write-Host ""

# --- 0. Настройки ---------------------------------------------------------------
if ($controllerMode -ne "distributed") {
    Show "WARN" "CONTROLLER_MODE=$controllerMode. Для схемы Android Controller + Windows Agent на ПК нужно CONTROLLER_MODE=distributed."
}
if ($controllerHost -eq "0.0.0.0") {
    Show "FAIL" "CONTROLLER_HOST=0.0.0.0: адрес для прослушивания, а не для подключения. На ПК укажите реальный IP устройства с контроллером (0.0.0.0 нужен только в .env самого контроллера)."
} elseif ($controllerHost -in @("127.0.0.1", "localhost")) {
    Show "WARN" "CONTROLLER_HOST=${controllerHost}: агент подключается к самому ПК. Это верно, только если Контроллер запущен на этом же ПК; для телефона укажите его IP."
}

# --- 1. Автозапуск ----------------------------------------------------------------
try {
    $startupDir = [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::Startup)
    $lnkPath = if ($startupDir) { Join-Path $startupDir "Telegram Remote Control.lnk" } else { "" }
    if ($lnkPath -and (Test-Path $lnkPath)) {
        $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($lnkPath)
        if (-not $lnk.TargetPath) {
            Show "FAIL" "Ярлык автозапуска пустой (нет TargetPath), при входе в Windows ничего не запустится. Переустановите: scripts\install_autostart.ps1"
        } elseif (-not (Test-Path $lnk.TargetPath)) {
            Show "FAIL" "Ярлык автозапуска указывает на несуществующий файл: $($lnk.TargetPath)"
        } else {
            Show "OK" "Ярлык автозапуска: $($lnk.TargetPath) $($lnk.Arguments)"
            if ($lnk.Arguments -match "-RequireWol") {
                Show "INFO" "Режим -RequireWol: при ручном включении ПК службы не стартуют (только после /wake). Для запуска всегда: install_autostart.ps1 -Always"
            }
        }
    } else {
        Show "WARN" "Ярлык автозапуска не найден (автозапуск не установлен): scripts\install_autostart.ps1"
    }
} catch {
    Show "WARN" "Не удалось проверить ярлык автозапуска: $($_.Exception.Message)"
}

# --- 2. Процессы ------------------------------------------------------------------
try {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -match "remote_control\.(agent|controller)\.main" })
    $agentProcs = @($procs | Where-Object { $_.CommandLine -match "remote_control\.agent\.main" })
    $botProcs = @($procs | Where-Object { $_.CommandLine -match "remote_control\.controller\.main" })
    if ($agentProcs.Count -gt 0) {
        Show "OK" "Windows Agent запущен (PID: $(($agentProcs | ForEach-Object { $_.ProcessId }) -join ', '))."
    } else {
        Show "FAIL" "Windows Agent не запущен. Запустите scripts\start_services.ps1 -Mode agent и смотрите лог ниже."
    }
    if ($botProcs.Count -gt 0) {
        Show "WARN" "На этом ПК запущен Controller/бот (PID: $(($botProcs | ForEach-Object { $_.ProcessId }) -join ', ')). Если тот же токен работает на телефоне, будет конфликт getUpdates."
    }
} catch {
    Show "WARN" "Не удалось получить список процессов: $($_.Exception.Message)"
}

# --- 3. Контроллер ------------------------------------------------------------------
$statusUrl = "http://${controllerHost}:${controllerPort}/api/v1/status"
$agentConnected = $false
try {
    $status = Invoke-RestMethod -Uri $statusUrl -Method Get -TimeoutSec 5 -ErrorAction Stop
    if ($status.agent_connected -eq $true) {
        $agentConnected = $true
        Show "OK" "Контроллер $statusUrl отвечает, агент подключён (agent_ready=$($status.agent_ready))."
    } else {
        Show "FAIL" "Контроллер $statusUrl отвечает, но агент НЕ подключён (agent_connected=false). Проверьте AGENT_AUTH_SECRET и лог агента ниже."
    }
} catch {
    Show "FAIL" "Контроллер $statusUrl недоступен: $($_.Exception.Message). Проверьте IP телефона, что контроллер запущен (control-bot.sh status) и что на телефоне CONTROLLER_HOST=0.0.0.0."
}

# --- 4. Логи -----------------------------------------------------------------------
foreach ($name in @("agent.log", "start_services.log")) {
    $path = Join-Path $LogDir $name
    Write-Host ""
    if (Test-Path $path) {
        Write-Host "--- $path (последние $TailLines строк) ---" -ForegroundColor Cyan
        Get-Content $path -Tail $TailLines
    } else {
        Write-Host "--- ${path}: файла нет (служба ещё ни разу не запускалась с этой версией) ---" -ForegroundColor DarkYellow
    }
}

Write-Host ""
if ($script:Problems -eq 0 -and $agentConnected) {
    Write-Host "ИТОГ: агент подключён к Контроллеру." -ForegroundColor Green
    exit 0
}
Write-Host "ИТОГ: найдено проблем: $script:Problems. Исправьте пункты FAIL сверху вниз." -ForegroundColor Red
exit 1
