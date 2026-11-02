param([switch]$NoOpen, [switch]$NoStart)
$ErrorActionPreference = 'Stop'
$DashboardHome = if ($env:SKILL_DASHBOARD_HOME) { $env:SKILL_DASHBOARD_HOME } else { Join-Path $env:LOCALAPPDATA 'SkillDashboard' }
$env:SKILL_DASHBOARD_HOME = $DashboardHome
$env:UV_UNMANAGED_INSTALL = Join-Path $DashboardHome 'uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $DashboardHome 'runtime'
$env:UV_PYTHON_INSTALL_BIN = '0'
New-Item -ItemType Directory -Force -Path $DashboardHome | Out-Null
$Uv = Join-Path $DashboardHome 'uv\uv.exe'
if (!(Test-Path $Uv)) { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression }
& $Uv python install 3.12 --no-registry --no-bin --no-config
if ($LASTEXITCODE -ne 0) { throw 'Could not provision the dashboard runtime.' }
$Python = (& $Uv python find --managed-python 3.12).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not find the dashboard runtime.' }
$Work = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $Work | Out-Null
try {
    Invoke-WebRequest https://github.com/jdjuan/skill-dashboard/releases/latest/download/skill-dashboard.zip -OutFile (Join-Path $Work 'app.zip')
    Expand-Archive (Join-Path $Work 'app.zip') (Join-Path $Work 'app')
    $Arguments = @((Join-Path $Work 'app\dashboard.py'), 'install', '--home', $DashboardHome)
    if ($NoOpen) { $Arguments += '--no-open' }
    if ($NoStart) { $Arguments += '--no-start' }
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Dashboard installation did not complete.' }
} finally { Remove-Item -Recurse -Force $Work }
