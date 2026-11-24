[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$InnoCompiler = (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$BuildDirectory = Join-Path $ProjectRoot "build"
$DistDirectory = Join-Path $ProjectRoot "dist"
$ReleaseDirectory = Join-Path $ProjectRoot "release"
$AppVersion = "1.6.5"
$ExpectedOutput = Join-Path $ReleaseDirectory "PiperReaderSetup-$AppVersion.exe"
$StandaloneOutput = Join-Path $ReleaseDirectory "PiperReaderStandalone-$AppVersion-x64.zip"
$ChecksumOutput = Join-Path $ReleaseDirectory "SHA256SUMS-$AppVersion.txt"
$DependencySourceOutput = Join-Path $ReleaseDirectory "PiperReader-$AppVersion-dependency-sources.zip"
$SourcesManifest = Join-Path $ProjectRoot "build_assets\sources\SOURCES.json"
$OcrAssetRoot = Join-Path $ProjectRoot "build_assets\ocr"
$OcrManifestPath = Join-Path $OcrAssetRoot "MODELS.json"
$OcrLicensePath = Join-Path $OcrAssetRoot "LICENSE"
$OcrModelRevision = "87416418657359cb625c412a48b6e1d6d41c29bd"

function Remove-KnownBuildDirectory {
    param([Parameter(Mandatory)][string]$Path)

    $resolved = [System.IO.Path]::GetFullPath($Path)
    $rootPrefix = $ProjectRoot.TrimEnd('\') + '\'
    $leaf = [System.IO.Path]::GetFileName($resolved)
    if (-not $resolved.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a directory outside the Piper Reader project: $resolved"
    }
    if ($leaf -notin @("build", "dist", "release")) {
        throw "Refusing to remove an unexpected directory: $resolved"
    }
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "piper_reader.pyw") -PathType Leaf)) {
    throw "Piper Reader source was not found in $ProjectRoot"
}
if (-not (Test-Path -LiteralPath $InnoCompiler -PathType Leaf)) {
    throw "Inno Setup compiler was not found at $InnoCompiler"
}

Push-Location $ProjectRoot
try {
    & $Python --version
    if ($LASTEXITCODE -ne 0) { throw "Python is unavailable." }
    & $Python (Join-Path $ProjectRoot "build_tools\prepare_native_audio.py")
    if ($LASTEXITCODE -ne 0) { throw "A verified source-built native audio decoder is required. See build_tools/native_audio/README.md." }
    & $Python -c "import PyInstaller, PIL, piper, kokoro_onnx, onnxruntime, pymupdf, edge_tts, soundfile, aiohttp, certifi"
    if ($LASTEXITCODE -ne 0) { throw "One or more build dependencies are unavailable." }
    & $Python (Join-Path $ProjectRoot "build_tools\check_online_dependencies.py")
    if ($LASTEXITCODE -ne 0) { throw "The pinned Microsoft speech protocol update is missing. Install requirements-online.txt." }
    if (-not (Test-Path -LiteralPath $OcrManifestPath -PathType Leaf)) {
        throw "The pinned OCR model manifest was not found at $OcrManifestPath"
    }
    $OcrManifest = Get-Content -LiteralPath $OcrManifestPath -Raw | ConvertFrom-Json
    if ($OcrManifest.revision -ne $OcrModelRevision) {
        throw "The OCR model manifest does not use the pinned revision $OcrModelRevision"
    }
    if (-not (Test-Path -LiteralPath $OcrLicensePath -PathType Leaf)) {
        throw "The OCR model license was not found at $OcrLicensePath"
    }
    foreach ($ModelProperty in $OcrManifest.models.PSObject.Properties) {
        $Model = $ModelProperty.Value
        $ModelPath = Join-Path $OcrAssetRoot "tessdata\$($Model.filename)"
        if (-not (Test-Path -LiteralPath $ModelPath -PathType Leaf)) {
            throw "The pinned OCR model was not found at $ModelPath"
        }
        $ModelItem = Get-Item -LiteralPath $ModelPath
        if ($ModelItem.Length -ne [long]$Model.size) {
            throw "The pinned OCR model has an unexpected size: $ModelPath"
        }
        $ModelHash = (Get-FileHash -LiteralPath $ModelPath -Algorithm SHA256).Hash
        if ($ModelHash -ne [string]$Model.sha256) {
            throw "The pinned OCR model failed its SHA-256 check: $ModelPath"
        }
    }
    & $Python -m py_compile (Join-Path $ProjectRoot "piper_reader.pyw") (Join-Path $ProjectRoot "browser_bridge.py") (Join-Path $ProjectRoot "library_store.py") (Join-Path $ProjectRoot "document_outline.py") (Join-Path $ProjectRoot "edge_engine.py") (Join-Path $ProjectRoot "clipboard_speech.py") (Join-Path $ProjectRoot "speech_diagnostics.py") (Join-Path $ProjectRoot "ai_studio.py") (Join-Path $ProjectRoot "ai_studio_ui.py") (Join-Path $ProjectRoot "lesson_audio.py")
    if ($LASTEXITCODE -ne 0) { throw "Piper Reader source compilation failed." }
    & $Python -m unittest discover -s (Join-Path $ProjectRoot "tests") -v
    if ($LASTEXITCODE -ne 0) { throw "Piper Reader regression tests failed." }

    Remove-KnownBuildDirectory -Path $BuildDirectory
    Remove-KnownBuildDirectory -Path $DistDirectory
    New-Item -ItemType Directory -Path $ReleaseDirectory -Force | Out-Null
    # Keep prior releases available for rollback. Replace only this version's
    # known generated deliverables when rebuilding it.
    foreach ($CurrentOutput in @($ExpectedOutput, $StandaloneOutput, $ChecksumOutput)) {
        if (Test-Path -LiteralPath $CurrentOutput) {
            Remove-Item -LiteralPath $CurrentOutput -Force
        }
    }

    & $Python (Join-Path $ProjectRoot "build_tools\create_icon.py")
    if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }
    & $Python (Join-Path $ProjectRoot "build_tools\collect_sources.py")
    if ($LASTEXITCODE -ne 0) { throw "Corresponding-source collection failed." }
    & $Python (Join-Path $ProjectRoot "build_tools\collect_licenses.py") --sources-manifest $SourcesManifest
    if ($LASTEXITCODE -ne 0) { throw "Third-party license collection failed." }

    & $Python -m PyInstaller --noconfirm --clean (Join-Path $ProjectRoot "PiperReader.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

    $PackagedExe = Join-Path $DistDirectory "PiperReader\PiperReader.exe"
    $PackagedRoot = Join-Path $DistDirectory "PiperReader"
    $PackagedExtension = Join-Path $PackagedRoot "_internal\browser_extension"
    $PackagedBridgeConfig = Join-Path $PackagedExtension "bridge-config.js"
    $PackagedBridgeTemplate = Join-Path $PackagedExtension "bridge-config.template.js"
    $BridgeTemplate = Join-Path $ProjectRoot "build_assets\bridge-config.template.js"
    if (-not (Test-Path -LiteralPath $PackagedExe -PathType Leaf)) {
        throw "PyInstaller did not create $PackagedExe"
    }
    $NativeAudioManifest = Get-Content -LiteralPath (Join-Path $ProjectRoot "build_assets\native_audio\build-manifest.json") -Raw | ConvertFrom-Json
    $PackagedAudioHash = (Get-FileHash -LiteralPath (Join-Path $PackagedRoot "_internal\_soundfile_data\libsndfile_x64.dll") -Algorithm SHA256).Hash
    if ($PackagedAudioHash -ne $NativeAudioManifest.dll_sha256) {
        throw "The frozen package does not contain the verified native audio decoder."
    }
    if (-not (Test-Path -LiteralPath $PackagedBridgeTemplate -PathType Leaf)) {
        throw "The packaged browser extension is missing its safe bridge template."
    }
    foreach ($ModelProperty in $OcrManifest.models.PSObject.Properties) {
        $Model = $ModelProperty.Value
        $PackagedModel = Join-Path $PackagedRoot "_internal\ocr\tessdata\$($Model.filename)"
        if (-not (Test-Path -LiteralPath $PackagedModel -PathType Leaf)) {
            throw "The packaged OCR model is missing: $PackagedModel"
        }
        $PackagedModelHash = (Get-FileHash -LiteralPath $PackagedModel -Algorithm SHA256).Hash
        if ($PackagedModelHash -ne [string]$Model.sha256) {
            throw "The packaged OCR model failed its SHA-256 check: $PackagedModel"
        }
    }
    foreach ($PackagedBase in @($PackagedRoot, (Join-Path $PackagedRoot "_internal"))) {
        foreach ($ForbiddenRelativePath in @("voices", "settings.json", "browser_bridge.json", "library", "library.db", "ai_lessons", "auth.json", "textbooks", "textbook_audio_cache", "gemini_api_key.dpapi", "speech_diagnostics.jsonl", "speech_diagnostics.jsonl.1")) {
            $ForbiddenPackagedPath = Join-Path $PackagedBase $ForbiddenRelativePath
            if (Test-Path -LiteralPath $ForbiddenPackagedPath) {
                throw "Private user data was unexpectedly packaged: $ForbiddenPackagedPath"
            }
        }
    }

    # Belt-and-suspenders sanitization: the staged extension always receives
    # the placeholder, even if a future spec change accidentally copies a token.
    Copy-Item -LiteralPath $BridgeTemplate -Destination $PackagedBridgeConfig -Force
    Remove-Item -LiteralPath $PackagedBridgeTemplate -Force
    $SanitizedConfig = Get-Content -LiteralPath $PackagedBridgeConfig -Raw
    if (-not $SanitizedConfig.Contains("__PIPER_TOKEN_GENERATED_ON_FIRST_RUN__")) {
        throw "The staged browser bridge configuration was not sanitized."
    }

    # Installer and portable archives share this exact distribution payload.
    $PackagedLicenses = Join-Path $PackagedRoot "licenses"
    New-Item -ItemType Directory -Path $PackagedLicenses -Force | Out-Null
    Copy-Item -Path (Join-Path $ProjectRoot "build_assets\licenses\*") -Destination $PackagedLicenses -Recurse -Force
    foreach ($NoticeName in @("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "SOURCE_CODE.md")) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $NoticeName) -Destination (Join-Path $PackagedRoot $NoticeName) -Force
    }
    Copy-Item -LiteralPath $SourcesManifest -Destination (Join-Path $PackagedLicenses "SOURCES.json") -Force
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "build_assets\native_audio\build-manifest.json") -Destination (Join-Path $PackagedLicenses "NATIVE_AUDIO_BUILD.json") -Force
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "INSTALLER_README.md") -Destination (Join-Path $PackagedRoot "INSTALLATION_AND_DISTRIBUTION.md") -Force
    if (-not (Test-Path -LiteralPath $DependencySourceOutput -PathType Leaf)) {
        throw "The companion dependency-source archive is missing."
    }

    # Exercise the actual windowed executable without opening its UI. The
    # isolated LocalAppData root proves that first-run data and extension
    # staging work without leaking the build computer's settings or token.
    $SmokeRoot = Join-Path $BuildDirectory "frozen-smoke"
    $SmokeLocalAppData = Join-Path $SmokeRoot "LocalAppData"
    $SmokeReport = Join-Path $SmokeRoot "report.json"
    New-Item -ItemType Directory -Path $SmokeLocalAppData -Force | Out-Null
    $PreviousLocalAppData = $env:LOCALAPPDATA
    $PreviousSelfTestReport = $env:PIPER_READER_SELF_TEST_REPORT
    try {
        $env:LOCALAPPDATA = $SmokeLocalAppData
        $env:PIPER_READER_SELF_TEST_REPORT = $SmokeReport
        $SmokeProcess = Start-Process -FilePath $PackagedExe -ArgumentList "--self-test" -WindowStyle Hidden -Wait -PassThru
        if ($SmokeProcess.ExitCode -ne 0) {
            throw "Frozen runtime self-test failed with exit code $($SmokeProcess.ExitCode)."
        }
    }
    finally {
        $env:LOCALAPPDATA = $PreviousLocalAppData
        $env:PIPER_READER_SELF_TEST_REPORT = $PreviousSelfTestReport
    }
    if (-not (Test-Path -LiteralPath $SmokeReport -PathType Leaf)) {
        throw "Frozen runtime self-test did not create its report."
    }
    $SmokeResult = Get-Content -LiteralPath $SmokeReport -Raw | ConvertFrom-Json
    if (-not $SmokeResult.passed -or -not $SmokeResult.checks.frozen) {
        throw "Frozen runtime self-test report did not pass. See $SmokeReport"
    }

    & $InnoCompiler (Join-Path $ProjectRoot "installer\PiperReader.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed." }
    if (-not (Test-Path -LiteralPath $ExpectedOutput -PathType Leaf)) {
        throw "Installer output was not found at $ExpectedOutput"
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $PackagedRoot,
        $StandaloneOutput,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )
    if (-not (Test-Path -LiteralPath $StandaloneOutput -PathType Leaf)) {
        throw "Standalone archive was not found at $StandaloneOutput"
    }

    $InstallerHash = Get-FileHash -LiteralPath $ExpectedOutput -Algorithm SHA256
    $StandaloneHash = Get-FileHash -LiteralPath $StandaloneOutput -Algorithm SHA256
    $DependencySourceHash = Get-FileHash -LiteralPath $DependencySourceOutput -Algorithm SHA256
    $ChecksumLines = @(
        "$($InstallerHash.Hash)  $([System.IO.Path]::GetFileName($ExpectedOutput))"
        "$($StandaloneHash.Hash)  $([System.IO.Path]::GetFileName($StandaloneOutput))"
        "$($DependencySourceHash.Hash)  $([System.IO.Path]::GetFileName($DependencySourceOutput))"
    )
    [System.IO.File]::WriteAllText(
        $ChecksumOutput,
        ($ChecksumLines -join "`n") + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )

    Write-Host ""
    Write-Host "Installer: $ExpectedOutput"
    Write-Host "SHA256:    $($InstallerHash.Hash)"
    Write-Host "Portable:  $StandaloneOutput"
    Write-Host "SHA256:    $($StandaloneHash.Hash)"
    Write-Host "Checksums: $ChecksumOutput"
}
finally {
    Pop-Location
}
