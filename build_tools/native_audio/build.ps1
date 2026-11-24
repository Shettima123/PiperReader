[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$WorkDirectory,
    [string]$Python = 'python',
    [string]$VisualStudioPath = '',
    [string]$CMake = '',
    [string]$Ninja = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$workRoot = [IO.Path]::GetFullPath($WorkDirectory)
$sourceRoot = Join-Path $workRoot 'src'
$prefix = Join-Path $workRoot 'prefix'
$output = Join-Path $workRoot 'output'
New-Item -ItemType Directory -Path $workRoot,$sourceRoot,$prefix,$output -Force | Out-Null

if (-not $VisualStudioPath) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $VisualStudioPath = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
}
if (-not $VisualStudioPath) { throw 'Visual Studio C++ x64 build tools are required.' }
$vsdev = Join-Path $VisualStudioPath 'Common7\Tools\VsDevCmd.bat'
$developerEnvironment = & $env:ComSpec /d /s /c ('call "' + $vsdev + '" -arch=x64 -host_arch=x64 >nul && set')
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize the Visual Studio x64 environment.' }
foreach ($entry in $developerEnvironment) {
    if ($entry -match '^([^=]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
    }
}
if (-not $CMake) { $CMake = Join-Path $VisualStudioPath 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe' }
if (-not $Ninja) { $Ninja = Join-Path $VisualStudioPath 'Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe' }
foreach ($tool in @($CMake,$Ninja)) { if (-not (Test-Path -LiteralPath $tool)) { throw "Build tool missing: $tool" } }

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable failed with exit code $LASTEXITCODE" }
}

$sources = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'sources.json') -Raw | ConvertFrom-Json
$extractCode = @'
import pathlib, sys, tarfile
archive, destination = map(pathlib.Path, sys.argv[1:])
with tarfile.open(archive) as source:
    source.extractall(destination, filter="data")
'@
foreach ($property in $sources.PSObject.Properties) {
    $source = $property.Value
    $archive = Join-Path $workRoot $source.filename
    if (-not (Test-Path -LiteralPath $archive)) { Invoke-WebRequest -Uri $source.url -OutFile $archive }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $source.sha256) { throw "Source checksum failed: $archive" }
    $unpacked = Join-Path $sourceRoot ($property.Name + '-' + $source.version)
    if (-not (Test-Path -LiteralPath $unpacked)) { Invoke-Checked $Python @('-c',$extractCode,$archive,$sourceRoot) }
    $noticeDir = Join-Path $output ('licenses\' + $property.Name + '-' + $source.version)
    New-Item -ItemType Directory -Path $noticeDir -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $unpacked $source.license_file) -Destination (Join-Path $noticeDir 'COPYING') -Force
}

# Preserve notices for the built-in codecs and upstream authors as well as the
# library-wide license. The C files retain their complete leading license text.
$additionalNotices = @{
    'libsndfile-1.2.2/AUTHORS' = 'libsndfile-1.2.2/AUTHORS'
    'libsndfile-1.2.2/src/GSM610/COPYRIGHT' = 'libsndfile-1.2.2/GSM610-COPYRIGHT'
    'libsndfile-1.2.2/src/G72x/g72x.c' = 'libsndfile-1.2.2/G72x-g72x.c'
    'libsndfile-1.2.2/src/ALAC/ALACBitUtilities.c' = 'libsndfile-1.2.2/ALAC-ALACBitUtilities.c'
    'mpg123-1.33.7/AUTHORS' = 'mpg123-1.33.7/AUTHORS'
    'lame-3.100/LICENSE' = 'lame-3.100/LICENSE'
    'lame-3.100/mpglib/AUTHORS' = 'lame-3.100/mpglib-AUTHORS'
}
foreach ($notice in $additionalNotices.GetEnumerator()) {
    Copy-Item -LiteralPath (Join-Path $sourceRoot $notice.Key) -Destination (Join-Path $output ('licenses/' + $notice.Value)) -Force
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'licenses/Apache-2.0.txt') -Destination (Join-Path $output 'licenses/libsndfile-1.2.2/Apache-2.0.txt') -Force

# Build the upstream LAME static library with its unmodified MSVC makefile.
# Command-line overrides select x64 and avoid obsolete code-generation flags.
$lameSource = Join-Path $sourceRoot 'lame-3.100'
Push-Location $lameSource
try {
    Invoke-Checked 'nmake.exe' @('/nologo','/f','Makefile.MSVC','MSVCVER=Win64','ASM=NO','MACHINE=/machine:X64','CC_OPTS=/nologo /O2 /MT /DFLOAT8=float /DREAL_IS_FLOAT=1 /DWin64 /W0','config.h','libmp3lame-static.lib')
} finally { Pop-Location }
New-Item -ItemType Directory -Path (Join-Path $prefix 'include\lame'),(Join-Path $prefix 'lib') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $lameSource 'include\lame.h') -Destination (Join-Path $prefix 'include\lame\lame.h') -Force
Copy-Item -LiteralPath (Join-Path $lameSource 'output\libmp3lame-static.lib') -Destination (Join-Path $prefix 'lib\libmp3lame-static.lib') -Force

$mpgBuild = Join-Path $workRoot 'build-mpg123'
Invoke-Checked $CMake @('-S',(Join-Path $sourceRoot 'mpg123-1.33.7\ports\cmake'),'-B',$mpgBuild,'-G','Ninja',"-DCMAKE_MAKE_PROGRAM=$Ninja",'-DCMAKE_BUILD_TYPE=Release','-DCMAKE_POLICY_DEFAULT_CMP0091=NEW','-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded',"-DCMAKE_INSTALL_PREFIX=$prefix",'-DBUILD_SHARED_LIBS=OFF','-DBUILD_LIBOUT123=OFF','-DBUILD_PROGRAMS=OFF','-DNETWORK=OFF')
Invoke-Checked $CMake @('--build',$mpgBuild,'--parallel','4')
Invoke-Checked $CMake @('--install',$mpgBuild)

$sndBuild = Join-Path $workRoot 'build-libsndfile'
Invoke-Checked $CMake @('-S',(Join-Path $sourceRoot 'libsndfile-1.2.2'),'-B',$sndBuild,'-G','Ninja',"-DCMAKE_MAKE_PROGRAM=$Ninja",'-DCMAKE_BUILD_TYPE=Release','-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded','-DCMAKE_SHARED_LINKER_FLAGS=shlwapi.lib',"-DCMAKE_PREFIX_PATH=$prefix",'-DBUILD_SHARED_LIBS=ON','-DBUILD_TESTING=OFF','-DBUILD_PROGRAMS=OFF','-DBUILD_EXAMPLES=OFF','-DENABLE_EXTERNAL_LIBS=OFF','-DENABLE_MPEG=ON','-DENABLE_EXPERIMENTAL=OFF','-DENABLE_CPACK=OFF','-DENABLE_PACKAGE_CONFIG=OFF','-DINSTALL_PKGCONFIG_MODULE=OFF')
if (-not (Select-String -LiteralPath (Join-Path $sndBuild 'src\config.h') -Pattern '^#define HAVE_MPEG 1$' -Quiet)) { throw 'MP3 support was not enabled.' }
Invoke-Checked $CMake @('--build',$sndBuild,'--parallel','4')
$builtDll = Join-Path $sndBuild 'sndfile.dll'
if (-not (Test-Path -LiteralPath $builtDll)) { throw 'Native libsndfile output is missing.' }
Copy-Item -LiteralPath $builtDll -Destination (Join-Path $output 'libsndfile_x64.dll') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'sources.json') -Destination (Join-Path $output 'sources.json') -Force
& 'dumpbin.exe' /dependents $builtDll | Set-Content -LiteralPath (Join-Path $output 'dll-dependencies.txt') -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw 'DLL dependency inspection failed.' }
$manifest = [ordered]@{
    architecture = 'Windows x64'
    library = 'libsndfile 1.2.2'
    mp3_decoder = 'mpg123 1.33.7'
    mp3_encoder = 'LAME 3.100'
    codecs = 'Built-in WAV and PCM formats; MP3 enabled; FLAC/Ogg/Vorbis/Opus disabled'
    source_changes = 'None; upstream build scripts with command-line options'
    msvc_runtime = 'Static (/MT)'
    dll_sha256 = (Get-FileHash -LiteralPath (Join-Path $output 'libsndfile_x64.dll') -Algorithm SHA256).Hash.ToLowerInvariant()
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output 'build-manifest.json') -Encoding utf8
Write-Output "Built native audio library: $(Join-Path $output 'libsndfile_x64.dll')"
