# Обратный SSH-туннель: прокси домашнего компьютера (127.0.0.1:10809) становится доступен серверу.
# Запуск: powershell -ExecutionPolicy Bypass -File deploy\tunnel.ps1 -Key "C:\путь\к\ключу.pem"
# Пока идёт цикл и компьютер включён, бот и Gemini на сервере работают. Подробности: docs/04-сервер-и-эксплуатация.md
param([Parameter(Mandatory = $true)][string]$Key, [string]$Server = "ubuntu@195.209.220.227", [int]$Port = 10809)

while ($true) {
  & ssh -i $Key -N -R "127.0.0.1:${Port}:127.0.0.1:${Port}" `
      -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o ConnectTimeout=20 $Server
  Write-Output "$(Get-Date -Format s) tunnel dropped (exit $LASTEXITCODE), reconnecting in 5s"
  Start-Sleep -Seconds 5
}
