@echo off
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command ^
  "$ErrorActionPreference = 'Stop'; $taskName = 'Local Signal Collector ' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value;" ^
  "$task = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue;" ^
  "if ($task) { if ($task.State -eq 'Running') { Stop-ScheduledTask -InputObject $task }; Unregister-ScheduledTask -InputObject $task -Confirm:$false };"
exit /b %ERRORLEVEL%
