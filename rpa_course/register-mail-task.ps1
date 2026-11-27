# Регистрирует ежеминутную проверку расписания автообработки в Планировщике Windows.
$ErrorActionPreference = 'Stop'

$taskName = 'OfisSnab-Mail'
$null = Get-Command docker.exe
$scriptHost = Join-Path ([Environment]::SystemDirectory) 'wscript.exe'
$project = [Security.SecurityElement]::Escape($PSScriptRoot)
$runner = [Security.SecurityElement]::Escape((Join-Path $PSScriptRoot 'run-mail-hidden.js'))
$userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$irkutskDate = [DateTimeOffset]::Now.ToOffset([TimeSpan]::FromHours(8)).ToString('yyyy-MM-dd')

$taskXml = @"
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <Repetition>
        <Interval>PT1M</Interval>
        <Duration>PT9H</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>${irkutskDate}T09:00:00+08:00</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Owner">
      <UserId>$userSid</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>false</StartWhenAvailable>
  </Settings>
  <Actions Context="Owner">
    <Exec>
      <Command>$scriptHost</Command>
      <Arguments>//B //NoLogo //E:JScript "$runner"</Arguments>
      <WorkingDirectory>$project</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

Register-ScheduledTask -TaskName $taskName -Xml $taskXml -Force | Out-Null
Get-ScheduledTaskInfo -TaskName $taskName
