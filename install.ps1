# Installs uv if missing, then the newest TFS release wheel, then runs `tfs setup`.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Fail($message) { Write-Host $message; exit 1 }

Write-Host '1/4 Checking uv'
if (-not (Get-Command uv -CommandType Application -ErrorAction SilentlyContinue)) {
    try { irm https://astral.sh/uv/install.ps1 | iex } catch { Fail 'Could not install uv.' }
    $uvDir = if ($env:UV_INSTALL_DIR) { $env:UV_INSTALL_DIR } else { "$HOME\.local\bin" }
    $env:Path = "$uvDir;$env:Path"
}

Write-Host '2/4 Finding the newest TFS release'
# releases/latest skips pre-releases, so read the list instead.
$wheel = $null
try {
    $release = @(irm 'https://api.github.com/repos/PrabhjotSodhi/training-for-something/releases?per_page=1')[0]
    $wheel = ($release.assets | Where-Object { $_.name -like '*.whl' } | Select-Object -First 1).browser_download_url
} catch { }
if (-not $wheel) { Fail 'Could not find the newest TFS release.' }

Write-Host '3/4 Installing TFS'
$toolDir = uv tool dir
if ($LASTEXITCODE -or -not $toolDir) { Fail 'Could not find the uv tool folder.' }
$python = Join-Path $toolDir 'training-for-something\Scripts\python.exe'
$helperCommand = '^"?' + [regex]::Escape($python) + '"?\s+.*\s+"?mcp"?\s*$'
$toolNames = @{
    'claude' = 'Claude Code'; 'codex' = 'Codex'; 'opencode' = 'OpenCode'; 'grok' = 'Grok Build'
    'agy' = 'Google Antigravity CLI'; 'kiro-cli' = 'Kiro CLI'; 'pi' = 'Pi'; 'muse' = 'Meta Muse Code'
}

function Get-TfsHelperNames {
    $processes = @{}
    foreach ($process in Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,Name,CommandLine) {
        $processes[$process.ProcessId] = $process
    }
    foreach ($process in $processes.Values) {
        if ($process.CommandLine -notmatch $helperCommand) { continue }
        $launcher = $processes[$process.ParentProcessId]
        if ($launcher.Name -ne 'tfs.exe') { continue }
        $parent = $processes[$launcher.ParentProcessId]
        $name = $toolNames[([string]$parent.Name -replace '\.exe$')]
        if ($name) { $name } else { 'An AI tool' }
    }
}

$announced = @()
while ($names = @(Get-TfsHelperNames | Sort-Object -Unique)) {
    foreach ($name in $names) {
        if ($name -notin $announced) { Write-Host "$name is using TFS. Close it, and the install continues." }
    }
    $announced = $names
    Start-Sleep -Seconds 1
}
uv tool install --force $wheel
if ($LASTEXITCODE) { Fail 'Could not install TFS.' }
uv tool update-shell
if ($LASTEXITCODE) { Fail 'Could not add TFS to PATH.' }

Write-Host '4/4 Starting tfs setup'
& (Join-Path (uv tool dir --bin) 'tfs.exe') setup
exit $LASTEXITCODE
