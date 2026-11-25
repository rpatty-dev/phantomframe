# PhantomFrame installer for Windows:
#   irm https://raw.githubusercontent.com/rpatty-dev/phantomframe/main/install.ps1 | iex
# Needs nothing installed first: no git, Python or admin rights. Re-run any time to update; settings.json is kept.
# Wrapped in a script block so an error stops the install without closing your PowerShell window.
& {
    # Not 'Stop': Windows PowerShell 5.1 turns any stderr from native tools into a fatal error.
    # Cmdlets use -ErrorAction Stop and native commands are checked with $LASTEXITCODE instead.
    $ErrorActionPreference = 'Continue'
    $ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow with the progress bar on
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $Repo = 'rpatty-dev/phantomframe'
    # GetFolderPath follows a Desktop moved into OneDrive
    $Dir = if ($env:PHANTOMFRAME_DIR) { $env:PHANTOMFRAME_DIR } else { Join-Path ([Environment]::GetFolderPath('Desktop')) 'PhantomFrame' }

    function Say($m) { Write-Host "==> $m" -ForegroundColor Magenta }
    function Get-Zip($url, $dest) {
        $tmp = Join-Path $env:TEMP ('phantomframe-' + [guid]::NewGuid())
        New-Item -ItemType Directory -Force -Path $tmp -ErrorAction Stop | Out-Null
        $zip = Join-Path $tmp 'download.zip'
        Invoke-WebRequest $url -OutFile $zip -UseBasicParsing -ErrorAction Stop
        Expand-Archive $zip -DestinationPath $dest -Force -ErrorAction Stop
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    Say "Downloading PhantomFrame to $Dir"
    $stage = Join-Path $env:TEMP ('phantomframe-src-' + [guid]::NewGuid())
    Get-Zip "https://github.com/$Repo/archive/refs/heads/main.zip" $stage
    New-Item -ItemType Directory -Force -Path $Dir -ErrorAction Stop | Out-Null
    Copy-Item -Path (Join-Path $stage 'phantomframe-main\*') -Destination $Dir -Recurse -Force -ErrorAction Stop
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue

    # uv (a single-file Python manager) downloads its own Python 3.12, so no system Python is needed
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) {
        $uv = Join-Path $Dir '.uv\uv.exe'
        if (-not (Test-Path $uv)) {
            Say 'Downloading uv (Python installer)'
            $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'aarch64' } else { 'x86_64' }
            Get-Zip "https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-pc-windows-msvc.zip" (Join-Path $Dir '.uv')
        }
    }

    Say 'Installing Python 3.12 and packages'
    $venvPy = Join-Path $Dir '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPy)) {
        & $uv venv -q --python 3.12 (Join-Path $Dir '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not install Python. Check your internet connection and re-run.' }
    }
    & $uv pip install -q --python $venvPy -r (Join-Path $Dir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Package install failed. Check your internet connection and re-run.' }

    # Optional: ffmpeg finds the capture card by name. Only installed when winget is available.
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        Say 'Installing ffmpeg'
        winget install -e --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements | Out-Host
    }

    Say 'Adding a Start menu shortcut'
    $pyw = Join-Path $Dir '.venv\Scripts\pythonw.exe'
    $app = '"' + (Join-Path $Dir 'gta6_framegen.py') + '"'
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath('Programs')) 'PhantomFrame.lnk'))
    $lnk.TargetPath = $pyw
    $lnk.Arguments = $app
    $lnk.WorkingDirectory = $Dir
    $lnk.IconLocation = Join-Path $Dir 'assets\icon.ico'
    $lnk.Save()

    Say "Installed to $Dir. Double-click Launch.bat there to run it later."
    if ($env:PHANTOMFRAME_NO_LAUNCH -ne '1') {
        Start-Process $pyw -ArgumentList $app -WorkingDirectory $Dir
    }
}
