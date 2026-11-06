@echo off
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command ^
  "$ErrorActionPreference = 'Stop'; $identity = [Security.Principal.WindowsIdentity]::GetCurrent(); $taskName = 'Local Signal Collector ' + $identity.User.Value;" ^
  "$exe = Join-Path (Get-Location) 'Local Signal.exe'; if (!(Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Local Signal.exe was not installed' };" ^
  "$user = $identity.Name;" ^
  "$action = New-ScheduledTaskAction -Execute $exe -Argument '--background' -WorkingDirectory (Get-Location).Path;" ^
  "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user;" ^
  "$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited;" ^
  "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1);" ^
  "Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null;"
exit /b %ERRORLEVEL%
