param(
    [string]$Python = 'python',
    [switch]$CreateUser,
    [switch]$Test,
    [switch]$Migrate,
    [string]$RestoreBackup,
    [switch]$ConfirmRestore,
    [int]$Port = 8877,
    [string]$DataPath = (Join-Path $PSScriptRoot 'data')
)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $taskModes = @($Test.IsPresent, $CreateUser.IsPresent, $Migrate.IsPresent, [bool]$RestoreBackup) | Where-Object { $_ }
    if (@($taskModes).Count -gt 1) { throw 'Test/CreateUser/Migrate/RestoreBackup 중 하나만 선택하세요.' }
    if ($Test) {
        & $Python -B -m unittest discover -s tests -v
    } elseif ($RestoreBackup) {
        if (-not $ConfirmRestore) { throw '서버를 종료하고 -ConfirmRestore로 백업 시점 복원을 승인하세요.' }
        & $Python -B server.py restore --data $DataPath --backup $RestoreBackup --confirm-restore
    } elseif ($Migrate) {
        & $Python -B server.py migrate --data $DataPath
    } elseif ($CreateUser) {
        & $Python -B server.py create-user --data $DataPath
    } else {
        & $Python -B server.py serve --data $DataPath --port $Port
    }
    if ($LASTEXITCODE -ne 0) { throw "실행 실패: 종료 코드 $LASTEXITCODE" }
} finally { Pop-Location }
