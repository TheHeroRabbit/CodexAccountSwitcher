param(
    [string]$BasePythonPath = "",
    [switch]$RuntimeOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$environmentRoot = Join-Path $projectRoot ".venv"
$localPython = Join-Path $environmentRoot "Scripts\python.exe"

if ((Test-Path -LiteralPath $environmentRoot) -and -not (Test-Path -LiteralPath $localPython -PathType Leaf)) {
    throw "Project .venv exists but is incomplete. Move or remove it explicitly, then run setup.ps1 again."
}

if (-not (Test-Path -LiteralPath $localPython -PathType Leaf)) {
    if ([string]::IsNullOrWhiteSpace($BasePythonPath)) {
        $pythonLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
        if ($null -eq $pythonLauncher) {
            throw "Python launcher was not found. Install Python 3.8 or pass -BasePythonPath."
        }
        Write-Host "Creating project environment with Python 3.8..."
        & $pythonLauncher.Source -3.8 -m venv $environmentRoot
    }
    else {
        if (-not (Test-Path -LiteralPath $BasePythonPath -PathType Leaf)) {
            throw "Base Python executable was not found: $BasePythonPath"
        }
        $resolvedBasePython = (Resolve-Path -LiteralPath $BasePythonPath).Path
        Write-Host "Creating project environment with: $resolvedBasePython"
        & $resolvedBasePython -m venv $environmentRoot
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create the project .venv."
    }
}

if (-not (Test-Path -LiteralPath $localPython -PathType Leaf)) {
    throw "Project Python executable was not created: $localPython"
}

$environmentRoot = (Resolve-Path -LiteralPath $environmentRoot).Path
$localPython = (Resolve-Path -LiteralPath $localPython).Path
$actualPrefix = (& $localPython -c "import sys; print(sys.prefix)").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($actualPrefix)) {
    throw "Unable to verify the project environment."
}
$expectedPrefix = [System.IO.Path]::GetFullPath($environmentRoot).TrimEnd("\")
$actualPrefix = [System.IO.Path]::GetFullPath($actualPrefix).TrimEnd("\")
if (-not $actualPrefix.Equals($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Python sys.prefix does not match the project .venv."
}

& $localPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 'Python 3.8 or newer is required.')"
if ($LASTEXITCODE -ne 0) {
    throw "Project Python must be version 3.8 or newer."
}

$requirementsName = if ($RuntimeOnly) { "requirements.txt" } else { "requirements-dev.txt" }
$requirementsPath = Join-Path $projectRoot $requirementsName
& $localPython -m pip install --disable-pip-version-check -r $requirementsPath
if ($LASTEXITCODE -ne 0) {
    throw "Unable to install dependencies into the project .venv."
}

& $localPython -c "import sys; print('Project Python:', sys.executable); print('Environment:', sys.prefix)"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to start Python from the project .venv."
}
Write-Host "Project environment is ready."
