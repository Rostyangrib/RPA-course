# Проверяет настройки на текущую минуту и при необходимости запускает цикл робота.
$ErrorActionPreference = 'Stop'

$irkutsk = (Get-Date).ToUniversalTime().AddHours(8)
$minutes = $irkutsk.Hour * 60 + $irkutsk.Minute - 540
if ($minutes -lt 0 -or $minutes -gt 540) { return }

$interval = 5
$settingsPath = Join-Path $PSScriptRoot '.runtime/schedule/settings.json'
if (Test-Path -LiteralPath $settingsPath) {
    $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $interval = $settings.interval_minutes
    if ($settings.enabled -isnot [bool] -or ($interval -isnot [int] -and $interval -isnot [long]) -or
        $interval -lt 1 -or $interval -gt 540) {
        throw 'Invalid mail schedule settings.'
    }
    if (-not $settings.enabled) { return }
}

if ($minutes % $interval -eq 0) {
    & docker.exe compose run --rm --no-deps -T ofissnab python -m rpa_course
    exit $LASTEXITCODE
}
