# Register (or remove) the logon task that keeps Engraphy running.
#
#   powershell -ExecutionPolicy Bypass -File register-task.ps1 -InstallDir "C:\...\Engraphy"
#   powershell -ExecutionPolicy Bypass -File register-task.ps1 -Remove
#
# Register-ScheduledTask takes the XML as a string, which is why the template is
# read and substituted here rather than handed to `schtasks /XML`: schtasks
# insists the file be UTF-16, and a template that has to be re-encoded before it
# can be read is a template nobody can edit safely.
#
# Runs as the installing user with no elevation. The task it creates runs as that
# same user, which is the whole point of the logon-task design: nothing here
# needs administrator rights, so nothing here can fail for want of them.

[CmdletBinding()]
param(
    [string]$InstallDir,
    [string]$TaskName = 'Engraphy',
    [switch]$Remove,
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        # Stop before unregistering: an unregistered task whose process is still
        # running leaves a server nothing can stop by name any more.
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "removed the $TaskName task"
    } else {
        Write-Output "no $TaskName task to remove"
    }
    return
}

if (-not $InstallDir) { throw 'InstallDir is required unless -Remove is given' }
$InstallDir = (Resolve-Path $InstallDir).Path.TrimEnd('\')

$template = Join-Path $PSScriptRoot 'engraphy-task.xml'
if (-not (Test-Path $template)) { throw "missing $template" }

# The task runs as, and is triggered by the logon of, whoever installed it.
# DOMAIN\user rather than a SID so the task is legible in Task Scheduler.
$userId = "$env:USERDOMAIN\$env:USERNAME"

$xml = (Get-Content $template -Raw).Replace('@INSTDIR@', $InstallDir).Replace('@USERID@', $userId)

# -Force replaces an existing registration, which is what an upgrade needs: the
# install directory may have moved, and the task has to point at the new one.
Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
Write-Output "registered the $TaskName task for $userId, running $InstallDir\engraphy-win.exe run"

if ($StartNow) {
    # An install should not require a logoff to become useful.
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "started $TaskName"
}
