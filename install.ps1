param(
    [string]$ReleaseArchiveUrl = "",
    [switch]$SkipImageBuild
)

$ErrorActionPreference = "Stop"
$PluginRoot = $PSScriptRoot
$VenvDir = Join-Path $PluginRoot ".venv"
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
$ReqFile = Join-Path $PluginRoot "requirements.txt"
$McpConfigPath = Join-Path $PluginRoot "mcp_config.json"
$ImagesDir = Join-Path $PluginRoot "images"
$ArchivePath = Join-Path $ImagesDir "whale-desktop-v1.tar.gz"
$DockerDir = Join-Path $PluginRoot "docker"
$ImageTag = "whale-desktop:v1"

Write-Host "[1/5] Checking Python 3.10+ on Windows..." -ForegroundColor Cyan
$pyLauncherArgs = @()
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pyCmd) {
    $pyExe = $pyCmd.Source
} else {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pyLauncher) {
        throw "Python 3.10+ was not found in PATH. Install Python from python.org first."
    }
    $pyExe = $pyLauncher.Source
    $pyLauncherArgs = @("-3")
}
$pyVerOut = & $pyExe @pyLauncherArgs -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$parts = $pyVerOut.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 10)) {
    throw "Python 3.10+ required (found $pyVerOut)."
}
Write-Host "  Found Python $pyVerOut via $pyExe" -ForegroundColor Gray

Write-Host "[2/5] Setting up isolated .venv and installing requirements.txt..." -ForegroundColor Cyan
if (-not (Test-Path $VenvPy)) {
    & $pyExe @pyLauncherArgs -m venv $VenvDir
}
& $VenvPy -m pip install --upgrade pip
& $VenvPy -m pip install -r $ReqFile
if ($LASTEXITCODE -ne 0) { throw "Failed to install Python dependencies from requirements.txt" }

Write-Host "[3/5] Updating mcp_config.json (UTF-8 without BOM) to use local .venv..." -ForegroundColor Cyan
$mcpCfg = @{
    mcpServers = @{
        "whale-desktop" = @{
            command = $VenvPy
            args = @('${PLUGIN_ROOT}\bin\whale_mcp.py')
            env = @{
                WHALE_DESKTOP_DATA = '${PLUGIN_DATA}'
                PYTHONIOENCODING = "utf-8"
                PYTHONUNBUFFERED = "1"
                PYTHONUTF8 = "1"
            }
        }
    }
}
$jsonText = $mcpCfg | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($McpConfigPath, $jsonText + "`n", (New-Object System.Text.UTF8Encoding($false)))

if (-not $SkipImageBuild) {
    Write-Host "[4/5] Checking Docker Desktop..." -ForegroundColor Cyan
    docker info --format "{{.ServerVersion}}" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Docker Desktop is not running. Start Docker Desktop manually and re-run install.ps1." -ForegroundColor Yellow
        exit 1
    }

    Write-Host "[5/5] Preparing Docker image ($ImageTag)..." -ForegroundColor Cyan
    if (-not (Test-Path $ImagesDir)) { New-Item -ItemType Directory -Path $ImagesDir | Out-Null }

    if ($ReleaseArchiveUrl -and -not (Test-Path $ArchivePath)) {
        Write-Host "  Downloading prebuilt release archive..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri $ReleaseArchiveUrl -OutFile $ArchivePath
    }

    if (Test-Path $ArchivePath) {
        Write-Host "  Loading Docker image from $ArchivePath..." -ForegroundColor Yellow
        & $VenvPy -c "import gzip, shutil, subprocess, sys; p = subprocess.Popen(['docker','load'], stdin=subprocess.PIPE); gz = gzip.open(sys.argv[1],'rb'); shutil.copyfileobj(gz, p.stdin); gz.close(); p.stdin.close(); sys.exit(p.wait())" $ArchivePath
        if ($LASTEXITCODE -ne 0) { throw "Failed to restore Docker image from $ArchivePath" }
    } else {
        Write-Host "  Building Docker image $ImageTag locally from docker/Dockerfile..." -ForegroundColor Yellow
        docker build -t $ImageTag $DockerDir
        if ($LASTEXITCODE -ne 0) { throw "Docker build failed." }
    }
} else {
    Write-Host "[4/5-5/5] Skipping Docker image build (-SkipImageBuild)." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Whale Desktop installation complete! Enable/reload the plugin in Antigravity Customizations." -ForegroundColor Green
