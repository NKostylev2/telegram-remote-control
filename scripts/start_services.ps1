<#
.SYNOPSIS
    Запуск сопутствующих сервисов (AG, Nova) и компонентов Telegram Remote Control на Windows.
.DESCRIPTION
    1. Запускает AdGuard/bypass ("M:\Downloads\AG_2.19.1.exe"), если не запущен.
    2. Запускает Nova ("M:\Nova\Nova.exe"), если не запущен.
    3. Ожидает готовности сетевого соединения / локального прокси.
    4. Запускает компонент Telegram Remote Control (Агент или Контроллер/Бот в зависимости от .env или параметра -Mode).
.PARAMETER Mode
    Режим запуска:
    - 'auto' (по умолчанию): определяет режим из .env (distributed -> agent, local -> bot).
    - 'agent': принудительно запустить Windows Agent (remote-control-agent).
    - 'controller' или 'bot': принудительно запустить Controller/Bot (remote-control-bot).
    - 'both': запустить и Контроллер, и Агент.
.PARAMETER DelaySeconds
    Пауза в секундах для стабилизации Nova/AG перед запуском Python-сервиса (по умолчанию 5 сек).
#>

[CmdletBinding()]
param(
    [ValidateSet("auto", "agent", "controller", "bot", "both")]
    [string]$Mode = "auto",

    [int]$DelaySeconds = 5
)

$ErrorActionPreference = "Continue"

$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$AgPath = "M:\Downloads\AG_2.19.1.exe"
$NovaPath = "M:\Nova\Nova.exe"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$EnvFile = Join-Path $ProjectRoot ".env"

function Write-Log([string]$Message, [string]$Level = "INFO") {
    $timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Write-Host "[$timestamp] [$Level] $Message"
}

Write-Log "=== Запуск сервисов Telegram Remote Control ==="

# 1. Запуск AG_2.19.1.exe
if (Test-Path $AgPath) {
    $agProcess = Get-Process -Name "AG_2.19.1" -ErrorAction SilentlyContinue
    if (-not $agProcess) {
        Write-Log "Запуск AG_2.19.1: '$AgPath'..."
        Start-Process -FilePath $AgPath -WindowStyle Minimized
        Write-Log "AG_2.19.1 успешно запущен."
    } else {
        Write-Log "AG_2.19.1 уже работает (PID: $($agProcess.Id -join ', '))."
    }
} else {
    Write-Log "Файл AG_2.19.1 не найден по пути '$AgPath'. Пропуск." "WARN"
}

# 2. Запуск Nova.exe
if (Test-Path $NovaPath) {
    $novaProcess = Get-Process -Name "Nova" -ErrorAction SilentlyContinue
    if (-not $novaProcess) {
        Write-Log "Запуск Nova: '$NovaPath'..."
        Start-Process -FilePath $NovaPath -WindowStyle Minimized
        Write-Log "Nova успешно запущена."
    } else {
        Write-Log "Nova уже работает (PID: $($novaProcess.Id -join ', '))."
    }
} else {
    Write-Log "Файл Nova не найден по пути '$NovaPath'. Пропуск." "WARN"
}

# 3. Ожидание инициализации сети и прокси
Write-Log "Ожидание инициализации сети ($DelaySeconds сек)..."
Start-Sleep -Seconds $DelaySeconds

# Проверка доступности локального прокси Nova (127.0.0.1:1372)
$proxyPort = 1372
$proxyReady = $false
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $iar = $tcp.BeginConnect("127.0.0.1", $proxyPort, $null, $null)
    $success = $iar.AsyncWaitHandle.WaitOne(2000, $false)
    if ($success -and $tcp.Connected) {
        $tcp.EndConnect($iar)
        $proxyReady = $true
    }
    $tcp.Close()
} catch {
    $proxyReady = $false
}

if ($proxyReady) {
    Write-Log "Локальный прокси Nova (127.0.0.1:$proxyPort) доступен."
} else {
    Write-Log "Предупреждение: Локальный прокси на порту $proxyPort пока не отвечает. Продолжение запуска..." "WARN"
}

# 4. Проверка Python окружения
if (-not (Test-Path $VenvPython)) {
    Write-Log "Критическая ошибка: Виртуальное окружение не найдено по пути '$VenvPython'." "ERROR"
    exit 1
}

# 5. Определение целевого компонента Remote Control
$resolvedMode = $Mode
if ($resolvedMode -eq "auto") {
    $controllerMode = "distributed"
    if (Test-Path $EnvFile) {
        $content = Get-Content $EnvFile
        foreach ($line in $content) {
            if ($line -match "^\s*CONTROLLER_MODE\s*=\s*(local|distributed)") {
                $controllerMode = $matches[1]
                break
            }
        }
    }
    if ($controllerMode -eq "distributed") {
        $resolvedMode = "agent"
    } else {
        $resolvedMode = "controller"
    }
    Write-Log "Режим auto определён по .env как: '$resolvedMode' (CONTROLLER_MODE=$controllerMode)."
}

# 6. Запуск выбранных процессов
Set-Location -Path $ProjectRoot

switch ($resolvedMode) {
    "agent" {
        Write-Log "Запуск Windows Agent (remote_control.agent.main)..."
        Start-Process -FilePath $VenvPython -ArgumentList "-m", "remote_control.agent.main" -WorkingDirectory $ProjectRoot
        Write-Log "Windows Agent запущен в фоновом процессе."
    }
    { $_ -in "controller", "bot" } {
        Write-Log "Запуск Controller Bot (remote_control.controller.main)..."
        Start-Process -FilePath $VenvPython -ArgumentList "-m", "remote_control.controller.main" -WorkingDirectory $ProjectRoot
        Write-Log "Controller Bot запущен в фоновом процессе."
    }
    "both" {
        Write-Log "Запуск Controller Bot и Windows Agent..."
        Start-Process -FilePath $VenvPython -ArgumentList "-m", "remote_control.controller.main" -WorkingDirectory $ProjectRoot
        Start-Sleep -Seconds 2
        Start-Process -FilePath $VenvPython -ArgumentList "-m", "remote_control.agent.main" -WorkingDirectory $ProjectRoot
        Write-Log "Оба сервиса (Bot и Agent) запущены."
    }
}

Write-Log "=== Все сервисы успешно инициализированы ==="
