# Обратный SSH-туннель: прокси домашнего компьютера (127.0.0.1:10809) становится доступен серверу.
# Запуск: powershell -ExecutionPolicy Bypass -File deploy\tunnel.ps1 -Key "C:\путь\к\ключу.pem"
# В фоне, без окна (переживает закрытие терминала, работает до выключения компьютера):
#   Start-Process powershell -WindowStyle Hidden -ArgumentList '-ExecutionPolicy Bypass -File deploy\tunnel.ps1 -Key "C:\путь\к\ключу.pem"'
# Пока идёт цикл и компьютер включён, бот и Gemini на сервере работают. Подробности: docs/04-сервер-и-эксплуатация.md
param(
  [Parameter(Mandatory = $true)][string]$Key,
  [string]$Server = "ubuntu@195.209.220.227",
  [int]$Port = 10809,
  [string]$Ssh = "",   # путь к ssh.exe; по умолчанию системный OpenSSH, а если его нет — ssh из Git for Windows
  [string]$Log = ""    # файл журнала переподключений (по умолчанию %LOCALAPPDATA%\afisha-tunnel.log)
)

if (-not $Ssh) {
  $found = Get-Command ssh -ErrorAction SilentlyContinue
  if ($found) { $Ssh = $found.Source }
  elseif (Test-Path "$env:ProgramFiles\Git\usr\bin\ssh.exe") { $Ssh = "$env:ProgramFiles\Git\usr\bin\ssh.exe" }
  else { throw "ssh не найден: установите OpenSSH (Параметры → Дополнительные компоненты) или Git for Windows, либо укажите -Ssh" }
}
if (-not $Log) { $Log = Join-Path $env:LOCALAPPDATA "afisha-tunnel.log" }

while ($true) {
  Add-Content -Path $Log -Value "$(Get-Date -Format s) connecting via $Ssh"
  & $Ssh -i $Key -N -R "127.0.0.1:${Port}:127.0.0.1:${Port}" `
      -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o ConnectTimeout=20 `
      -o BatchMode=yes -o StrictHostKeyChecking=accept-new $Server
  Add-Content -Path $Log -Value "$(Get-Date -Format s) tunnel dropped (exit $LASTEXITCODE), reconnecting in 5s"
  Start-Sleep -Seconds 5
}
