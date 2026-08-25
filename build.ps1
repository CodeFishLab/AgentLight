param(
    [switch]$SkipDependencyInstall,
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
$VenvRoot = Join-Path $ProjectRoot '.venv'
$PythonExe = Join-Path $VenvRoot 'Scripts\python.exe'
$BuildRoot = Join-Path $ProjectRoot 'build'
$DistRoot = Join-Path $ProjectRoot 'dist'
$ReleaseRoot = Join-Path $ProjectRoot 'release'
$CacheRoot = Join-Path $ProjectRoot '.cache'

$env:PIP_CACHE_DIR = Join-Path $CacheRoot 'pip'
$env:PYTHONPYCACHEPREFIX = Join-Path $CacheRoot 'pycache'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $CacheRoot 'pyinstaller'
$env:TEMP = Join-Path $CacheRoot 'tmp'
$env:TMP = $env:TEMP
$env:AGENTLIGHT_DATA_DIR = Join-Path $CacheRoot 'test-data'

New-Item -ItemType Directory -Path $BuildRoot, $DistRoot, $ReleaseRoot, $env:PIP_CACHE_DIR, $env:PYTHONPYCACHEPREFIX, $env:PYINSTALLER_CONFIG_DIR, $env:TEMP, $env:AGENTLIGHT_DATA_DIR -Force | Out-Null

if (-not (Test-Path -LiteralPath $PythonExe)) {
    py -3 -m venv $VenvRoot
}

if (-not $SkipDependencyInstall) {
    & $PythonExe -m pip install --upgrade pip
    & $PythonExe -m pip install -r (Join-Path $ProjectRoot 'requirements-dev.txt')
}


$PytestTemp = Join-Path $env:TEMP ('pytest-' + [guid]::NewGuid().ToString('N'))
& $PythonExe -m pytest (Join-Path $ProjectRoot 'tests') --basetemp $PytestTemp -p no:cacheprovider
if ($LASTEXITCODE -ne 0) {
    throw "Test failed, build stopped"
}

$IconFile = Join-Path $ProjectRoot 'assets\agentlight.ico'
if (-not (Test-Path -LiteralPath $IconFile)) {
    throw "Missing icon resources: $IconFile"
}

$GuiWork = Join-Path $BuildRoot 'gui'
$CtlWork = Join-Path $BuildRoot 'ctl'
$CtlDist = Join-Path $BuildRoot 'ctl-dist'
$GuiDist = Join-Path $DistRoot 'AgentLight'
$WebUiData = '{0};agentlight/webui' -f (Join-Path $ProjectRoot 'src\agentlight\webui')
$IconData = '{0};assets' -f $IconFile

if (Test-Path -LiteralPath $GuiWork) { Remove-Item -LiteralPath $GuiWork -Recurse -Force }
if (Test-Path -LiteralPath $CtlWork) { Remove-Item -LiteralPath $CtlWork -Recurse -Force }
if (Test-Path -LiteralPath $CtlDist) { Remove-Item -LiteralPath $CtlDist -Recurse -Force }
if (Test-Path -LiteralPath $GuiDist) { Remove-Item -LiteralPath $GuiDist -Recurse -Force }

& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name AgentLight `
    --icon $IconFile `
    --paths (Join-Path $ProjectRoot 'src') `
    --hidden-import hid `
    --add-data $WebUiData `
    --add-data $IconData `
    --exclude-module PySide6 `
    --exclude-module shiboken6 `
    --exclude-module tkinter `
    --distpath $DistRoot `
    --workpath $GuiWork `
    --specpath (Join-Path $BuildRoot 'spec') `
(Join-Path $ProjectRoot 'launcher_gui.py')


& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --console `
    --name agentlightctl `
    --icon $IconFile `
    --paths (Join-Path $ProjectRoot 'src') `
    --exclude-module PySide6 `
    --exclude-module shiboken6 `
    --exclude-module tkinter `
    --exclude-module aiohttp `
    --distpath $CtlDist `
    --workpath $CtlWork `
    --specpath (Join-Path $BuildRoot 'spec') `
(Join-Path $ProjectRoot 'launcher_ctl.py')

Copy-Item -LiteralPath (Join-Path $CtlDist 'agentlightctl\agentlightctl.exe') -Destination (Join-Path $GuiDist 'agentlightctl.exe') -Force

$CtlInternal = Join-Path $CtlDist 'agentlightctl\_internal'
$GuiInternal = Join-Path $GuiDist '_internal'
Get-ChildItem -LiteralPath $CtlInternal -Recurse -File | ForEach-Object {
    $relative = $_.FullName.Substring($CtlInternal.Length + 1)
    $target = Join-Path $GuiInternal $relative
    if (-not (Test-Path -LiteralPath $target)) {
        $parent = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Copy-Item -LiteralPath $_.FullName -Destination $target
    }
}
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'README.md') -Destination (Join-Path $GuiDist 'README.md') -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'LICENSE') -Destination (Join-Path $GuiDist 'LICENSE') -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'scripts\agentlightctl-hook.ps1') -Destination (Join-Path $GuiDist 'agentlightctl-hook.ps1') -Force

if (-not $SkipInstaller) {
    $RegistryKeys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1'
    )
    $FromRegistry = foreach ($Key in $RegistryKeys) {
        try {
            $Location = (Get-ItemProperty -Path $Key -ErrorAction Stop).InstallLocation
            if ($Location) { Join-Path $Location 'ISCC.exe' }
        }
        catch { }
    }
    $Candidates = @(
        $env:ISCC_PATH
        $FromRegistry
        (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    $Iscc = $Candidates | Select-Object -First 1
    if (-not $Iscc) {
        throw "Inno Setup 6 not found. Please install first, or point to ISCC.exe using the ISCC_PATH environment variable."
    }
    & $Iscc (Join-Path $ProjectRoot 'installer\AgentLight.iss')
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup compilation failed. Exit code: $LASTEXITCODE"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $ReleaseRoot 'AgentLightSetup.exe'))) {
        throw "Inno Setup did not generate AgentLightSetup.exe."
    }
    Write-Host "Build complete: $ReleaseRoot\AgentLightSetup.exe"
}
else {
    Write-Host "Application build complete: $GuiDist"
}
