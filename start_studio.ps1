# Opens camctl Studio (visual camera control) in your browser.
#   This PC only:            powershell -ExecutionPolicy Bypass -File start_studio.ps1
#   Also iPad/phone (Wi-Fi): powershell -ExecutionPolicy Bypass -File start_studio.ps1 -Lan
#   Change the iPad PIN:     powershell -ExecutionPolicy Bypass -File start_studio.ps1 -SetPin
# With -Lan, the first start asks you to choose a PIN, and Windows may ask whether
# Python may accept connections: allow it on Private networks only.
# Close this window to stop it.
param([switch]$Lan, [switch]$SetPin)
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $Py)) { $Py = (Get-Command python).Source }
Set-Location $Root
$a = @("-m", "camctl.webui")
if ($Lan) { $a += "--lan" }
if ($SetPin) { $a += "--set-pin" }
# a Studio already running in the other mode (laptop-only vs home network) is stopped cleanly first
& $Py -m camctl.launch $(if ($Lan) { "--lan" } else { "--local" })
if ($LASTEXITCODE -eq 3) { Write-Host "Studio is already running this way."; exit 0 }
& $Py @a
