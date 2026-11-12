[CmdletBinding()]
param(
    [switch]$Collect,
    [switch]$Start,
    [ValidateRange(1, 65535)]
    [int]$Port = 8787
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" 2>$null
        if ($LASTEXITCODE -eq 0) { return @("py", "-3") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        & python -c "import sys; raise SystemExit(sys.version_info < (3, 10))" 2>$null
        if ($LASTEXITCODE -eq 0) { return @("python") }
    }
    throw "Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ and retry."
}

$Bootstrap = @(Find-Python)
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    $Launcher = $Bootstrap[0]
    if ($Bootstrap.Count -eq 2) {
        $LauncherOption = $Bootstrap[1]
        & $Launcher $LauncherOption -m venv .venv
    } else {
        & $Launcher -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the virtual environment." }
}

& $VenvPython run.py doctor --port $Port
if ($LASTEXITCODE -ne 0) { throw "Installation health check failed." }

if ($Collect) {
    & $VenvPython run.py daily
    if ($LASTEXITCODE -ne 0) { throw "Initial public market-data collection failed." }
}

if ($Start) {
    $OutLog = Join-Path $Root ".dash.log"
    $ErrLog = Join-Path $Root ".dash-error.log"
    $Process = Start-Process -FilePath $VenvPython `
        -ArgumentList @("-u", "run.py", "dash", "--host", "127.0.0.1", "--port", $Port) `
        -WorkingDirectory $Root -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog -PassThru
    Set-Content -Path (Join-Path $Root ".dash.pid") -Value $Process.Id
    Start-Sleep -Seconds 2
    & $VenvPython run.py doctor --port $Port --require-running
    if ($LASTEXITCODE -ne 0) { throw "Dashboard did not pass the health check." }
    Write-Host "Dashboard: http://127.0.0.1:$Port/"
    Write-Host "Logs: $OutLog and $ErrLog"
} else {
    Write-Host ""
    Write-Host "Setup complete. Start the dashboard with:"
    Write-Host "  .\.venv\Scripts\python.exe run.py dash"
    Write-Host "Dashboard: http://127.0.0.1:$Port/"
}
