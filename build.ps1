param(
    [string]$VenvPath = "C:\venv",
    [string]$PythonPath = "",
    [switch]$SkipTests,
    [switch]$SkipRepositoryCheck
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$appName = "Codex" + [char]0x8D26 + [char]0x53F7 + [char]0x5207 + [char]0x6362 + [char]0x5668
$appScript = Join-Path $projectRoot "app.py"
$iconPath = Join-Path $projectRoot "assets\app_icon.ico"
$appIconPath = Join-Path $projectRoot "assets\app_icon.png"
$headerIconPath = Join-Path $projectRoot "assets\app_icon_64.png"
$modelsPath = Join-Path $projectRoot "assets\deepseek_models.json"
$versionPath = Join-Path $projectRoot "version_info.txt"
$distPath = Join-Path $projectRoot "dist"
$workPath = Join-Path $projectRoot "build\pyinstaller"
$specPath = Join-Path $projectRoot "build\spec"

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $pythonExecutable = Join-Path $VenvPath "Scripts\python.exe"
}
else {
    $pythonExecutable = $PythonPath
}

if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "Python executable was not found: $pythonExecutable"
}
$pythonExecutable = (Resolve-Path -LiteralPath $pythonExecutable).Path

Push-Location $projectRoot
try {
    & $pythonExecutable -c "import sys; print('Build Python:', sys.executable); import PyInstaller, PIL, tomlkit; print('PyInstaller:', PyInstaller.__version__); print('Pillow:', PIL.__version__); print('tomlkit:', tomlkit.__version__)"
    if ($LASTEXITCODE -ne 0) {
        throw "Build dependencies are missing. Install requirements-build.txt first."
    }

    if (-not $SkipRepositoryCheck) {
        & $pythonExecutable ".\tools\repository_check.py"
        if ($LASTEXITCODE -ne 0) {
            throw "Repository checks failed; packaging was stopped."
        }
    }

    & $pythonExecutable ".\tools\prepare_icon.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Icon preparation failed."
    }

    if (-not $SkipTests) {
        & $pythonExecutable -m unittest discover -s ".\tests" -v
        if ($LASTEXITCODE -ne 0) {
            throw "Tests failed; packaging was stopped."
        }
    }

    New-Item -ItemType Directory -Force -Path $distPath, $workPath, $specPath | Out-Null

    & $pythonExecutable -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --noupx `
        --name $appName `
        --icon $iconPath `
        --add-data ($appIconPath + ";assets") `
        --add-data ($headerIconPath + ";assets") `
        --add-data ($modelsPath + ";assets") `
        --version-file $versionPath `
        --distpath $distPath `
        --workpath $workPath `
        --specpath $specPath `
        $appScript

    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller packaging failed."
    }

    Write-Host ("Build completed: " + (Join-Path $projectRoot ("dist\" + $appName + ".exe")))
}
finally {
    Pop-Location
}
