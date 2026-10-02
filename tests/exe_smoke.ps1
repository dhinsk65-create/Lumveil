param(
    [Parameter(Mandatory=$true)][string]$Bundle,
    [Parameter(Mandatory=$true)][string]$Video,
    [Parameter(Mandatory=$true)][string]$WorkDir,
    [switch]$CorruptSettings
)
$ErrorActionPreference = 'Stop'
$bundlePath = (Resolve-Path -LiteralPath $Bundle).Path
$videoPath = (Resolve-Path -LiteralPath $Video).Path
$workPath = [IO.Path]::GetFullPath($WorkDir)
[IO.Directory]::CreateDirectory($workPath) | Out-Null
$profilePath = Join-Path $workPath 'profile'
$settingsPath = Join-Path $profilePath 'Lumveil'
[IO.Directory]::CreateDirectory($settingsPath) | Out-Null
$utf8 = [Text.UTF8Encoding]::new($false)
$playerSettings = @{restore_manual_settings=$false; auto_update_checks=$false;
    playback_eof_action='stop'; resume_enabled=$false}
if ($CorruptSettings) {
    $playerSettings.resume_enabled = $true
    $playerSettings.resume_positions = @()
    $playerSettings.recent_files = $null
    $playerSettings.volume = 'invalid'
}
$gpuSettings = @{scale='bilinear'; cscale='bilinear'; deband=$false;
    sigmoid=$false; correct_ds=$false; interpolate=$false; hwdec='no';
    deinterlace=$false; amf_frc=$false; glsl=@()}
[IO.File]::WriteAllText((Join-Path $settingsPath 'player_settings.json'),
    ($playerSettings | ConvertTo-Json), $utf8)
[IO.File]::WriteAllText((Join-Path $settingsPath 'gpu_settings_mpv.json'),
    ($gpuSettings | ConvertTo-Json), $utf8)
[IO.File]::WriteAllText((Join-Path $settingsPath 'window_settings.json'),
    '{"geometry":"960x580+30+30"}', $utf8)
$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $bundlePath
$startInfo.UseShellExecute = $false
$startInfo.WorkingDirectory = Split-Path -Parent $bundlePath
$startInfo.ArgumentList.Add($videoPath)
$startInfo.Environment['APPDATA'] = $profilePath
$startInfo.Environment['MPV_HOME'] = (Join-Path $workPath 'mpv-profile')
[IO.Directory]::CreateDirectory($startInfo.Environment['MPV_HOME']) | Out-Null
$result = [ordered]@{target=$bundlePath; started=(Get-Date -Format o);
    settings_isolated=$true; video=$videoPath; hwdec='no'; interpolation=$false;
    correction=$false; corrupt_settings_test=[bool]$CorruptSettings;
    bootloader='WIP'; gpu_counter='WIP'; close='WIP'}
$playerProcess = $null
try {
    if (Get-Process Lumveil -ErrorAction SilentlyContinue) {
        throw 'An existing Lumveil instance is running; refusing to interfere.'
    }
    $playerProcess = [Diagnostics.Process]::Start($startInfo)
    $result.pid = $playerProcess.Id
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 100
        $playerProcess.Refresh()
        if ($playerProcess.HasExited) { throw "EXE exited during startup: $($playerProcess.ExitCode)" }
    } while ($playerProcess.MainWindowHandle -eq 0 -and [DateTime]::UtcNow -lt $deadline)
    if ($playerProcess.MainWindowHandle -eq 0) { throw 'No main window appeared.' }
    $result.bootloader = 'PASS'
    $result.window_title = $playerProcess.MainWindowTitle
    try {
        $counterSets = @(Get-Counter '\GPU Engine(*)\Utilization Percentage' -SampleInterval 1 -MaxSamples 6 -ErrorAction Stop)
        $samples = @()
        foreach ($counterSet in $counterSets) {
            $matching = @($counterSet.CounterSamples | Where-Object {
                $_.InstanceName -like "pid_$($playerProcess.Id)_*" -and $_.InstanceName -like '*engtype_3D*'
            })
            $samples += [double](($matching | Measure-Object CookedValue -Sum).Sum)
        }
        $result.gpu_3d_percent = $samples
        if (($samples | Measure-Object -Maximum).Maximum -gt 0) { $result.gpu_counter = 'PASS' }
        else { $result.gpu_counter = 'NO_ACTIVITY_OBSERVED' }
    } catch {
        $result.gpu_counter = 'UNAVAILABLE'
        $result.gpu_counter_error = $_.Exception.Message
        Start-Sleep -Seconds 6
    }
    $playerProcess.Refresh()
    if ($playerProcess.HasExited) { throw "EXE exited during playback: $($playerProcess.ExitCode)" }
    if (-not $playerProcess.CloseMainWindow()) { throw 'Could not request a normal window close.' }
    if (-not $playerProcess.WaitForExit(10000)) { throw 'EXE did not close within ten seconds.' }
    $result.exit_code = $playerProcess.ExitCode
    if ($playerProcess.ExitCode -ne 0) { throw "Nonzero EXE exit: $($playerProcess.ExitCode)" }
    $result.close = 'PASS'
    $savedSettings = Get-Content -LiteralPath (Join-Path $settingsPath 'player_settings.json') -Raw | ConvertFrom-Json
    if ($savedSettings.recent_files -notcontains $videoPath) { throw 'Test file was not recorded in the isolated settings.' }
    if ($CorruptSettings) {
        if ($savedSettings.resume_positions -isnot [pscustomobject]) {
            throw 'Corrupt resume positions were not normalized to an object.'
        }
        $result.corrupt_settings_normalized = 'PASS'
    }
    $result.media_open_recorded = 'PASS'
    $result.result = 'PASS'
} catch {
    $result.result = 'FAIL'
    $result.error = $_.Exception.Message
    throw
} finally {
    if ($playerProcess -and -not $playerProcess.HasExited) {
        $playerProcess.CloseMainWindow() | Out-Null
        if (-not $playerProcess.WaitForExit(3000)) {
            $playerProcess.Kill()
            $result.forced_test_process_cleanup = $true
        }
    }
    $result.ended = Get-Date -Format o
    [IO.File]::WriteAllText((Join-Path $workPath 'results.json'), ($result | ConvertTo-Json -Depth 5), $utf8)
    $result | ConvertTo-Json -Depth 5
}
