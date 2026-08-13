param(
    [string]$VenvPath = "",
    [string]$PythonPath = "",
    [switch]$SkipTests,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$buildScript = Join-Path $projectRoot "build.ps1"
$appName = "Codex" + [char]0x8D26 + [char]0x53F7 + [char]0x5207 + [char]0x6362 + [char]0x5668
$accountFolderName = -join @(
    [char]0x8D26,
    [char]0x53F7,
    [char]0x6587,
    [char]0x4EF6
)
$usageGuideName = -join @(
    [char]0x4F7F,
    [char]0x7528,
    [char]0x8BF4,
    [char]0x660E,
    ".txt"
)
$changeNotesName = -join @(
    [char]0x66F4,
    [char]0x65B0,
    [char]0x8BF4,
    [char]0x660E,
    ".txt"
)
$accountGuideName = $accountFolderName + (-join @(
    [char]0x8BF4,
    [char]0x660E,
    ".txt"
))
$doNotShareName = -join @(
    [char]0x8BF7,
    [char]0x52FF,
    [char]0x5206,
    [char]0x4EAB,
    [char]0x6B64,
    [char]0x6587,
    [char]0x4EF6,
    [char]0x5939,
    ".txt"
)

$buildArguments = @{}
if (-not [string]::IsNullOrWhiteSpace($VenvPath)) {
    $buildArguments["VenvPath"] = $VenvPath
}
if (-not [string]::IsNullOrWhiteSpace($PythonPath)) {
    $buildArguments["PythonPath"] = $PythonPath
}
if ($SkipTests) {
    $buildArguments["SkipTests"] = $true
}
& $buildScript @buildArguments

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    if ([string]::IsNullOrWhiteSpace($VenvPath)) {
        $environmentRoot = Join-Path $projectRoot ".venv"
    }
    elseif ([System.IO.Path]::IsPathRooted($VenvPath)) {
        $environmentRoot = [System.IO.Path]::GetFullPath($VenvPath)
    }
    else {
        $environmentRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $VenvPath))
    }
    $pythonExecutable = Join-Path $environmentRoot "Scripts\python.exe"
}
else {
    $pythonExecutable = $PythonPath
}
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "Python executable was not found after the build: $pythonExecutable"
}
$pythonExecutable = (Resolve-Path -LiteralPath $pythonExecutable).Path

Push-Location $projectRoot
try {
    $appVersion = (& $pythonExecutable -c "import app; print(app.APP_VERSION)").Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($appVersion)) {
        throw "Unable to read the application version."
    }

    $releaseRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "release"))
    $projectPrefix = [System.IO.Path]::GetFullPath($projectRoot).TrimEnd("\") + "\"
    if (-not $releaseRoot.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Release path escaped the project root."
    }

    $bundleName = "$appName-v$appVersion"
    $archiveName = "CodexAccountSwitcher-v$appVersion"
    $bundlePath = Join-Path $releaseRoot $bundleName
    $zipPath = Join-Path $releaseRoot ($archiveName + ".zip")
    $zipHashPath = $zipPath + ".sha256.txt"

    foreach ($target in @($bundlePath, $zipPath, $zipHashPath)) {
        if (Test-Path -LiteralPath $target) {
            if (-not $Force) {
                throw "Release output already exists: $target. Use -Force to replace it."
            }
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }

    New-Item -ItemType Directory -Path $bundlePath | Out-Null
    $accountFolder = Join-Path $bundlePath $accountFolderName
    New-Item -ItemType Directory -Path $accountFolder | Out-Null

    $executablePath = Join-Path $projectRoot ("dist\" + $appName + ".exe")
    Copy-Item -LiteralPath $executablePath -Destination $bundlePath
    Copy-Item -LiteralPath (Join-Path ".\packaging" $usageGuideName) -Destination $bundlePath
    Copy-Item -LiteralPath (Join-Path ".\packaging" $changeNotesName) -Destination $bundlePath
    Copy-Item -LiteralPath (Join-Path ".\packaging" $accountGuideName) `
        -Destination (Join-Path $accountFolder $doNotShareName)

    $executableHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $executablePath).Hash
    $hashText = $executableHash + "  " + $appName + ".exe" + [Environment]::NewLine
    [System.IO.File]::WriteAllText(
        (Join-Path $bundlePath "SHA256.txt"),
        $hashText,
        [System.Text.UTF8Encoding]::new($false)
    )

    Compress-Archive -LiteralPath $bundlePath -DestinationPath $zipPath -CompressionLevel Optimal
    $zipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash
    [System.IO.File]::WriteAllText(
        $zipHashPath,
        ($zipHash + "  " + [System.IO.Path]::GetFileName($zipPath) + [Environment]::NewLine),
        [System.Text.UTF8Encoding]::new($false)
    )

    Write-Host "Release bundle completed: $zipPath"
}
finally {
    Pop-Location
}
