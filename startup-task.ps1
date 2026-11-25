[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [ValidateSet('Install','Status','Remove')][string]$Action = 'Status',
    [string]$Python,
    [string]$DataPath = (Join-Path $PSScriptRoot 'data'),
    [ValidateRange(1024,65535)][int]$Port = 8877,
    [string]$TaskName = 'ActivitySupportLocalServer',
    [string]$RunAs = ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
)
$ErrorActionPreference = 'Stop'
Import-Module ScheduledTasks -ErrorAction Stop
$taskMarker = 'ActivitySupportLocalServer:v1'
$serverPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'server.py'))
$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($Action -eq 'Status') {
    if (-not $existing) { Write-Output '자동 시작 작업이 등록되지 않았습니다.'; return }
    $existing | Select-Object TaskName,State,Description,Actions,Principal,Triggers
    Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath '\' | Select-Object LastRunTime,LastTaskResult,NextRunTime
    return
}
if ($existing -and ($existing.Description -ne "$taskMarker|$serverPath")) {
    throw '같은 이름의 다른 작업이 있습니다. 덮어쓰거나 제거하지 않습니다.'
}
if ($Action -eq 'Remove') {
    if (-not $existing) { Write-Output '등록된 작업이 없습니다.'; return }
    if ($existing.State -eq 'Running') { throw '작업이 실행 중입니다. 운영자가 서버를 정상 종료한 뒤 해제하세요. 프로세스를 강제 종료하지 않습니다.' }
    if ($PSCmdlet.ShouldProcess($TaskName,'부팅 자동 시작 등록 해제 (데이터 보존)')) {
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath '\' -Confirm:$false
    }
    return
}
if (-not $Python) { throw '-Python에 상시 설치된 python.exe의 절대 경로를 지정하세요.' }
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$dataRoot = [IO.Path]::GetFullPath($DataPath)
if ($pythonPath -match '\\WindowsApps\\') { throw '앱 업데이트로 경로가 바뀌는 WindowsApps Python 대신 일반 설치 Python을 지정하세요.' }
foreach ($taskPath in @($pythonPath,$dataRoot,$serverPath)) {
    if ($taskPath.StartsWith('\\') -or $taskPath -match '["\r\n]') { throw '인용부호/개행 없는 로컬 디스크 절대 경로만 지원합니다.' }
}
if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) {
    throw '먼저 run.ps1 -CreateUser로 같은 데이터 경로에 계정을 생성하세요. 부팅 등록 중 빈 DB를 만들지 않습니다.'
}
$dbPath = Join-Path $dataRoot 'development.sqlite3'
if (-not (Test-Path -LiteralPath $dbPath -PathType Leaf)) { throw '기존 DB가 없습니다. 데이터 경로를 확인하세요.' }
if ((Get-Item -LiteralPath $dataRoot).Attributes -band [IO.FileAttributes]::Encrypted) { throw 'S4U 작업은 EFS 암호화 폴더를 지원하지 않습니다.' }
& $pythonPath -B -c "import sys,sqlite3; assert sys.version_info >= (3,11); print('Python runtime OK')"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 이상과 sqlite3가 필요합니다.' }
$taskArguments = '-B -u "' + $serverPath + '" serve --data "' + $dataRoot + '" --port ' + $Port + ' --log-dir "' + (Join-Path $dataRoot 'logs') + '"'
$taskAction = New-ScheduledTaskAction -Execute $pythonPath -Argument $taskArguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT30S'
# Local files only. No password saved and no elevated server privileges.
$principal = New-ScheduledTaskPrincipal -UserId $RunAs -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
if ($PSCmdlet.ShouldProcess("$TaskName ($RunAs / $dataRoot)",'부팅 30초 후 로컬 서버 시작 등록')) {
    if ($existing -and $existing.State -eq 'Running') { throw '실행 중 작업의 설정은 변경하지 않습니다. 서버를 정상 종료하세요.' }
    Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Action $taskAction -Trigger $trigger `
        -Principal $principal -Settings $settings -Description "$taskMarker|$serverPath" -Force | Out-Null
    Write-Output '부팅 자동 시작 등록 완료. 현재 서버를 강제 실행·종료하지 않았습니다.'
    Write-Output "로그: $(Join-Path $dataRoot 'logs\server.log')"
    Write-Output '현재 기동 시험은 운영자가 Start-ScheduledTask로 실행하고 상태/로그를 확인하세요.'
}
