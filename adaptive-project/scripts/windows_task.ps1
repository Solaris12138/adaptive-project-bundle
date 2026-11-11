<#
Observe or delete exactly one Windows Task Scheduler task. This script never
creates tasks, elevates privileges, or writes receipts to disk. Preserve stdout,
stderr and the process exit code at the caller. Exit 0 means verified observation
or deletion; exit 2 means refusal; exit 1 means unknown/error.

Dot-source this file to test Invoke-ExactWindowsTask with an injected service.
The production command line deliberately has no service-injection parameter.
#>
[CmdletBinding()]
param(
    [string]$TaskPath,
    [string]$Action = 'Observe'
)

function Get-ExactTaskMatches {
    [CmdletBinding()]
    param($Folder, [string]$ExpectedPath)
    $ErrorActionPreference = 'Stop'
    # TASK_ENUM_HIDDEN = 1. Hidden tasks must participate in absence checks.
    $collection = $Folder.GetTasks(1)
    if ($null -eq $collection) { throw 'Task enumeration returned a null collection.' }
    $count = $collection.Count
    if ($null -eq $count -or [int]$count -lt 0) { throw 'Invalid task enumeration count.' }
    $matches = @()
    for ($i = 1; $i -le [int]$count; $i++) {
        $task = $collection.Item($i)
        if ($null -eq $task -or [string]::IsNullOrEmpty([string]$task.Path)) {
            throw 'Task enumeration returned an item without a full path.'
        }
        if ([string]::Equals([string]$task.Path, $ExpectedPath, [StringComparison]::OrdinalIgnoreCase)) {
            $matches += $task
        }
    }
    return @{ Matches = $matches; Count = [int]$count }
}

function New-WindowsTaskError {
    param([string]$Stage, [System.Exception]$Exception)
    $chain = @()
    $current = $Exception
    while ($null -ne $current) {
        $chain += [ordered]@{
            type = $current.GetType().FullName
            message = $current.Message
            hresult = $current.HResult
            hresult_hex = ('0x{0:X8}' -f $current.HResult)
        }
        $current = $current.InnerException
    }
    return [ordered]@{
        stage = $Stage
        type = $Exception.GetType().FullName
        message = $Exception.Message
        hresult = $Exception.HResult
        hresult_hex = ('0x{0:X8}' -f $Exception.HResult)
        exception_chain = $chain
    }
}

function Get-KnownWindowsTaskState {
    param($Task)
    $ErrorActionPreference = 'Stop'
    $rawState = $Task.State
    $state = 0
    if ($null -eq $rawState -or -not [int]::TryParse([string]$rawState, [ref]$state) -or
        $state -lt 1 -or $state -gt 4) {
        throw 'Task Scheduler did not return a known task state; operation refused.'
    }
    return $state
}

function Invoke-ExactWindowsTask {
    [CmdletBinding()]
    param(
        [string]$TaskPath,
        [string]$Action = 'Observe',
        [scriptblock]$ServiceFactory = { New-Object -ComObject 'Schedule.Service' }
    )
    $ErrorActionPreference = 'Stop'
    $report = [ordered]@{
        schema = 'adaptive-project.windows-task.v1'
        task_path = $TaskPath
        action = $Action
        started_at = [DateTimeOffset]::UtcNow.ToString('o')
        finished_at = $null
        status = 'unknown'
        verified_absent = $null
        task = $null
        enumeration_count_before = $null
        enumeration_count_after = $null
        delete_attempted = $false
        delete_returned = $false
        error = $null
        exit_code = 1
    }
    $stage = 'validate'
    try {
        if ($Action -cne 'Observe' -and $Action -cne 'Delete') {
            throw [ArgumentException]::new('Action must be Observe or Delete.')
        }
        if ([string]::IsNullOrWhiteSpace($TaskPath) -or
            -not $TaskPath.StartsWith('\') -or $TaskPath.EndsWith('\') -or
            $TaskPath -match '[*?\[\]/\x00-\x1f]' -or $TaskPath.Contains('\\')) {
            throw [ArgumentException]::new('TaskPath must be an exact absolute task path, never a root, folder, wildcard or relative path.')
        }
        $segments = $TaskPath.Substring(1).Split([char]'\')
        foreach ($segment in $segments) {
            if ([string]::IsNullOrWhiteSpace($segment) -or $segment -eq '.' -or $segment -eq '..') {
                throw [ArgumentException]::new('TaskPath contains an invalid path segment.')
            }
        }
        $split = $TaskPath.LastIndexOf('\')
        $folderPath = '\'
        if ($split -gt 0) { $folderPath = $TaskPath.Substring(0, $split) }
        $taskName = $TaskPath.Substring($split + 1)
        $stage = 'create_service'
        $service = & $ServiceFactory
        if ($null -eq $service) { throw 'Task Scheduler service factory returned null.' }
        $stage = 'connect'
        [void]$service.Connect()
        $stage = 'get_folder'
        $folder = $service.GetFolder($folderPath)
        if ($null -eq $folder) { throw 'Task Scheduler returned a null folder.' }
        $stage = 'enumerate_before'
        $before = Get-ExactTaskMatches -Folder $folder -ExpectedPath $TaskPath
        $report.enumeration_count_before = $before.Count
        if ($before.Matches.Count -gt 1) { throw 'Enumeration returned multiple exact matches.' }
        if ($before.Matches.Count -eq 0) {
            $report.status = 'absent'
            $report.verified_absent = $true
            $report.exit_code = 0
        } else {
            $stage = 'read_target'
            $target = $before.Matches[0]
            $report.task = [ordered]@{
                path = [string]$target.Path
                state = Get-KnownWindowsTaskState -Task $target
                last_task_result = $target.LastTaskResult
                last_run_time = $target.LastRunTime.ToString('o')
            }
            $report.status = 'present'
            $report.verified_absent = $false
            $report.exit_code = 0
            if ($Action -ceq 'Delete') {
                $stage = 'delete_precondition'
                # Read state again immediately before deletion. Scheduler does not
                # offer an atomic delete-if-not-running operation.
                $report.task.state = Get-KnownWindowsTaskState -Task $target
                if ($report.task.state -eq 4 -or $report.task.state -eq 2) {
                    $report.error = New-WindowsTaskError -Stage $stage -Exception ([InvalidOperationException]::new('Target task is running or queued; deletion refused.'))
                    $report.exit_code = 2
                } else {
                    $stage = 'delete'
                    $report.delete_attempted = $true
                    [void]$folder.DeleteTask($taskName, 0)
                    $report.delete_returned = $true
                    $stage = 'enumerate_after'
                    $after = Get-ExactTaskMatches -Folder $folder -ExpectedPath $TaskPath
                    $report.enumeration_count_after = $after.Count
                    if ($after.Matches.Count -ne 0) { throw 'Target remains after deletion.' }
                    $report.status = 'absent'
                    $report.verified_absent = $true
                }
            }
        }
    } catch {
        $report.status = 'unknown'
        $report.verified_absent = $null
        $report.exit_code = 1
        $report.error = New-WindowsTaskError -Stage $stage -Exception $_.Exception
    }
    $report.finished_at = [DateTimeOffset]::UtcNow.ToString('o')
    return [pscustomobject]$report
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    $result = Invoke-ExactWindowsTask -TaskPath $TaskPath -Action $Action
    $result | ConvertTo-Json -Depth 12 -Compress
    exit $result.exit_code
}
