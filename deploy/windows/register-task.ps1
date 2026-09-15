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

# Comments go BEFORE substitution, and that order is the whole point.
#
# Task Scheduler's XML parser is not .NET's, and it rejects the leading comment
# block that documents this template. Worse, substituting first can make the
# document invalid XML outright: an install path containing a double hyphen ends
# up inside a comment, and `--` is illegal there. Stripping first means the
# substituted values never land anywhere a parser cares about their punctuation.
#
# The declaration goes too. Register-ScheduledTask takes a .NET string, which is
# UTF-16 in memory, and the parser refuses a document declaring anything else:
#     The task XML is malformed. (1,40)::ERROR: unable to switch the encoding
# Reserialising from DocumentElement drops it.
#
# The file on disk keeps both, because that file is the documentation for why
# each setting is what it is, and a template nobody can read is a template
# nobody can maintain.
[xml]$doc = Get-Content $template -Raw
foreach ($c in @($doc.SelectNodes('//comment()'))) { [void]$c.ParentNode.RemoveChild($c) }
$xml = $doc.DocumentElement.OuterXml.Replace('@INSTDIR@', $InstallDir).Replace('@USERID@', $userId)

# -Force replaces an existing registration, which is what an upgrade needs: the
# install directory may have moved, and the task has to point at the new one.
# -ErrorAction Stop because a Register-ScheduledTask failure is a NON-TERMINATING
# CIM error: without it the cmdlet reports the failure, the script carries on,
# and the caller is told the task was registered when it was not.
Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force -ErrorAction Stop | Out-Null

# Read it back. The registration above can report success for a task the service
# then declines to keep, and an install that quietly ends with no autostart is
# the one failure this whole design exists to prevent.
$registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $registered) { throw "$TaskName was not registered" }
Write-Output "registered the $TaskName task for $userId, running $InstallDir\engraphy-win.exe run"

if ($StartNow) {
    # An install should not require a logoff to become useful.
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "started $TaskName"
}
