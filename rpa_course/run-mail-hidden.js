// Запускает PowerShell без видимого окна и передаёт код завершения Планировщику.
var shell = WScript.CreateObject('WScript.Shell');
var powershell = shell.ExpandEnvironmentStrings('%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe');
var runner = WScript.ScriptFullName.replace(/[^\\]+$/, 'run-mail-scheduled.ps1');
var command = '"' + powershell + '" -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "' + runner + '"';

WScript.Quit(shell.Run(command, 0, true));
