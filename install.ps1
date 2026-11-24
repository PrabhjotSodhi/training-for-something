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
uv tool install --force $wheel
if ($LASTEXITCODE) { Fail 'Could not install TFS.' }
uv tool update-shell
if ($LASTEXITCODE) { Fail 'Could not add TFS to PATH.' }

Write-Host '4/4 Starting tfs setup'
& (Join-Path (uv tool dir --bin) 'tfs.exe') setup
exit $LASTEXITCODE
