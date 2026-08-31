<#
.SYNOPSIS
    Build Magoo's Windows release artifacts.

.DESCRIPTION
    Produces both downloads from one PyInstaller onedir tree:

        dist\MagooSetup-<version>.exe     per-user installer (needs Inno Setup)
        dist\magoo-<version>-win64.zip    portable

    The only difference between them is the portable marker file, which tells
    config._resolve_data_dir to keep user data beside the executable instead
    of in %LOCALAPPDATA%\Magoo.

    Build from a CLEAN virtualenv. The build environment dominates output
    size far more than anything in the spec — a stray dev dependency can
    multiply it.

.EXAMPLE
    .\packaging\build.ps1
    .\packaging\build.ps1 -SkipInstaller
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [switch]$SkipSelfTest
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$pyinstaller = Join-Path $root '.venv\Scripts\pyinstaller.exe'

if (-not (Test-Path $python)) {
    throw "No virtualenv at $python. Create one and run: pip install -e "".[test]"""
}

Push-Location $root
try {
    $version = & $python -c "import magoo; print(magoo.__version__)"
    if (-not $?) { throw 'could not read magoo.__version__' }
    Write-Host "Building Magoo $version" -ForegroundColor Cyan

    # --- 1. Windows file-properties resource, generated so it cannot drift
    & $python (Join-Path $PSScriptRoot 'make_version_info.py')
    if (-not $?) { throw 'version_info generation failed' }

    # --- 2. Freeze
    $dist = Join-Path $root 'dist'
    $tree = Join-Path $dist 'Magoo'

    # Clear the previous tree, with retries: this project lives under
    # OneDrive, which intermittently holds directory handles. PyInstaller
    # hits the same lock, prints a PermissionError traceback, and then
    # STILL EXITS 0 while leaving the previous build in place — so a stale
    # executable is the failure mode to defend against here.
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        if (-not (Test-Path $tree)) { break }
        Remove-Item $tree -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $tree) { Start-Sleep -Milliseconds 400 }
    }
    if (Test-Path $tree) {
        throw "could not clear $tree (a file is locked - close Magoo, and pause OneDrive sync)"
    }

    $startedAt = Get-Date
    & $pyinstaller (Join-Path $PSScriptRoot 'magoo.spec') --noconfirm `
        --distpath $dist --workpath (Join-Path $root 'build')
    if (-not $?) { throw 'PyInstaller failed' }

    $exe = Join-Path $tree 'Magoo.exe'
    if (-not (Test-Path $exe)) { throw "no executable at $exe" }
    # Belt and braces against that exit-0-on-failure case.
    if ((Get-Item $exe).LastWriteTime -lt $startedAt) {
        throw "$exe is stale - PyInstaller reported success without rebuilding"
    }

    # --- 3. Prove the frozen build actually works
    # A build can start fine and still be broken: SciPy reaches HiGHS through
    # a dynamic import PyInstaller cannot see, so a missing hidden import
    # only shows up when a user plans a run. Never ship without this.
    if (-not $SkipSelfTest) {
        Write-Host 'Running frozen self-test...' -ForegroundColor Cyan
        $probe = Join-Path $env:TEMP ("magoo-selftest-" + [guid]::NewGuid())
        $env:MAGOO_DATA_DIR = $probe
        try {
            # Start-Process -Wait, NOT the call operator. Magoo.exe is built
            # for the WINDOWS subsystem (console=False), and PowerShell's &
            # does not wait for GUI-subsystem processes — it returns at once,
            # leaves $LASTEXITCODE holding the PREVIOUS command's value, and
            # lets the script race ahead to Compress-Archive while the test is
            # still starting. That silently turned this gate into a no-op that
            # reported success for any build at all.
            # NO -NoNewWindow on purpose. That flag hands the child our
            # console, which is the one condition a double-clicked build
            # never has — and running the gate with a console is precisely
            # why the logging-recursion crash reached a user. Launched
            # without it, a windows-subsystem exe gets no console, exactly
            # like a double-click. Its stdout goes nowhere, which is fine:
            # the log file below is the evidence, and it is now mandatory.
            $proc = Start-Process -FilePath $exe -ArgumentList '--selftest' `
                -PassThru -Wait
            $code = $proc.ExitCode
            $log = Join-Path $probe 'logs\magoo.log'
            # A missing log means the process never really ran, which must not
            # read as success.
            if (-not (Test-Path $log)) {
                throw "frozen self-test produced no log at $log - it did not run"
            }
            Get-Content $log | Select-Object -Last 12
            if ($code -ne 0) { throw "frozen self-test FAILED (exit $code)" }
            Write-Host 'Self-test passed.' -ForegroundColor Green
        }
        finally {
            Remove-Item Env:\MAGOO_DATA_DIR -ErrorAction SilentlyContinue
            if (Test-Path $probe) { Remove-Item $probe -Recurse -Force -ErrorAction SilentlyContinue }
        }
    }

    # --- 4. Portable zip (the marker selects data-beside-the-exe)
    $marker = Join-Path $tree 'magoo-portable.txt'
    Set-Content -Path $marker -Encoding utf8 -Value @'
This file makes Magoo portable: it keeps your data in the "data" folder
beside Magoo.exe instead of in %LOCALAPPDATA%\Magoo.

Delete it if you would rather share data with an installed copy of Magoo.
'@
    $zip = Join-Path $dist "magoo-$version-win64.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path $tree -DestinationPath $zip -CompressionLevel Optimal
    Write-Host "Portable: $zip" -ForegroundColor Green

    # The installed build must NOT be portable, or an uninstall could take
    # the user's database with it.
    Remove-Item $marker -Force

    # --- 5. Installer
    if (-not $SkipInstaller) {
        # Find any installed Inno Setup, newest first. Do NOT hardcode a
        # version directory: the installer is compiled by whatever the
        # machine happens to have, and pinning "Inno Setup 6" silently
        # skipped the whole step on a machine with 7.
        $isccPath = (Get-Command 'iscc.exe' -ErrorAction SilentlyContinue).Source
        if (-not $isccPath) {
            $candidates = @()
            foreach ($base in @("${env:ProgramFiles(x86)}", $env:ProgramFiles)) {
                if (-not $base) { continue }
                Get-ChildItem -Path $base -Filter 'Inno Setup*' -Directory `
                    -ErrorAction SilentlyContinue | ForEach-Object {
                        $exe = Join-Path $_.FullName 'ISCC.exe'
                        if (Test-Path $exe) {
                            $n = 0
                            if ($_.Name -match '(\d+)') { $n = [int]$Matches[1] }
                            $candidates += [pscustomobject]@{ Path = $exe; V = $n }
                        }
                    }
            }
            $isccPath = ($candidates | Sort-Object V -Descending |
                Select-Object -First 1).Path
        }
        if ($isccPath) {
            Write-Host "Compiling installer with $isccPath" -ForegroundColor Cyan
            $setup = Join-Path $dist "MagooSetup-$version.exe"
            if (Test-Path $setup) { Remove-Item $setup -Force }
            & $isccPath "/DMyAppVersion=$version" `
                (Join-Path $PSScriptRoot 'magoo.iss') | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Inno Setup failed (exit $LASTEXITCODE)"
            }
            if (-not (Test-Path $setup)) {
                throw "Inno Setup reported success but $setup is missing"
            }
            Write-Host "Installer: $setup" -ForegroundColor Green
        }
        else {
            Write-Warning 'Inno Setup not found - skipping the installer.'
            Write-Warning 'Install it from https://jrsoftware.org/isdl.php'
        }
    }

    Write-Host ''
    Write-Host "Magoo $version artifacts in $dist" -ForegroundColor Cyan
    Get-ChildItem $dist -File | Format-Table Name, @{
        Name = 'Size'; Expression = { '{0:N1} MB' -f ($_.Length / 1MB) }
    }
}
finally {
    Pop-Location
}
