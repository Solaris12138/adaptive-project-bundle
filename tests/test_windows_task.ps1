[CmdletBinding()]
param([string]$HelperPath)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrEmpty($HelperPath)) {
    $HelperPath = Join-Path $PSScriptRoot '../adaptive-project/scripts/windows_task.ps1'
    if (-not (Test-Path -LiteralPath $HelperPath)) {
        $HelperPath = Join-Path $PSScriptRoot '../scripts/windows_task.ps1'
    }
}
. $HelperPath

function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

function New-FakeService {
    param([string[]]$Paths = @(), [int]$State = 3,
        [switch]$ConnectDenied, [switch]$FolderDenied, [int]$EnumerationDeniedAt = 0,
        [switch]$DeleteDenied, [switch]$DeleteNoOp)
    $tasks = New-Object System.Collections.ArrayList
    foreach ($path in $Paths) {
        [void]$tasks.Add([pscustomobject]@{
            Path = $path; State = $State; LastTaskResult = 267009
            LastRunTime = [datetime]::Parse('2026-11-12T02:03:04.1234567', [Globalization.CultureInfo]::InvariantCulture)
        })
    }
    $folder = [pscustomobject]@{
        Tasks = $tasks; EnumerationCalls = 0; EnumerationDeniedAt = $EnumerationDeniedAt
        Deleted = @(); DeleteDenied = [bool]$DeleteDenied; DeleteNoOp = [bool]$DeleteNoOp; Flags = @()
    }
    $folder | Add-Member ScriptMethod GetTasks {
        param($flags)
        $this.EnumerationCalls++
        $this.Flags += $flags
        if ($this.EnumerationCalls -eq $this.EnumerationDeniedAt) {
            throw [UnauthorizedAccessException]::new('Enumeration denied.')
        }
        $collection = [pscustomobject]@{ Count = $this.Tasks.Count; Items = @($this.Tasks.ToArray()) }
        $collection | Add-Member ScriptMethod Item { param($index) return $this.Items[$index - 1] }
        return $collection
    }
    $folder | Add-Member ScriptMethod DeleteTask {
        param($name, $flags)
        $this.Deleted += $name
        if ($this.DeleteDenied) { throw [UnauthorizedAccessException]::new('Deletion denied.') }
        if (-not $this.DeleteNoOp) {
            for ($i = $this.Tasks.Count - 1; $i -ge 0; $i--) {
                $candidate = $this.Tasks[$i].Path
                if ($candidate.Substring($candidate.LastIndexOf('\') + 1) -ceq $name) {
                    $this.Tasks.RemoveAt($i)
                }
            }
        }
    }
    $service = [pscustomobject]@{
        Folder = $folder; ConnectDenied = [bool]$ConnectDenied; FolderDenied = [bool]$FolderDenied
        Connected = $false; FolderCalls = @()
    }
    $service | Add-Member ScriptMethod Connect {
        if ($this.ConnectDenied) {
            # Deliberately nonterminating without caller's Stop policy.
            Write-Error -Message 'Connect access denied.' -Category PermissionDenied
        }
        $this.Connected = $true
    }
    $service | Add-Member ScriptMethod GetFolder {
        param($path)
        $this.FolderCalls += $path
        if ($this.FolderDenied) { throw [UnauthorizedAccessException]::new('Folder denied.') }
        return $this.Folder
    }
    return $service
}

$passed = @()
function Test-Case([string]$Name, [scriptblock]$Body) {
    & $Body
    $script:passed += $Name
}

Test-Case 'connect nonterminating denial becomes unknown and stops' {
    $fake = New-FakeService -ConnectDenied
    $r = Invoke-ExactWindowsTask '\Target' -ServiceFactory { $fake }
    Assert-True ($r.status -eq 'unknown' -and $null -eq $r.verified_absent -and $r.exit_code -eq 1) 'denial is not absence'
    Assert-True ($r.error.stage -eq 'connect' -and $fake.FolderCalls.Count -eq 0) 'never read folder after denied connect'
    Assert-True ($null -ne $r.error.hresult -and $r.error.exception_chain.Count -gt 0) 'structured error has HRESULT'
}
Test-Case 'folder denial is unknown' {
    $fake = New-FakeService -FolderDenied
    $r = Invoke-ExactWindowsTask '\Nested\Target' -ServiceFactory { $fake }
    Assert-True ($r.error.stage -eq 'get_folder' -and $null -eq $r.verified_absent -and $r.exit_code -ne 0) 'folder denial unknown'
}
Test-Case 'enumeration denial is unknown' {
    $fake = New-FakeService -EnumerationDeniedAt 1
    $r = Invoke-ExactWindowsTask '\Target' -ServiceFactory { $fake }
    Assert-True ($r.error.stage -eq 'enumerate_before' -and $null -eq $r.verified_absent -and $r.exit_code -ne 0) 'enumeration denial unknown'
}
Test-Case 'absence requires successful exact parent enumeration including hidden tasks' {
    $fake = New-FakeService -Paths @('\Nested\TargetOther', '\Nested\Other')
    $r = Invoke-ExactWindowsTask '\Nested\Target' -ServiceFactory { $fake }
    Assert-True ($r.status -eq 'absent' -and $r.verified_absent -eq $true -and $r.exit_code -eq 0) 'precise absence'
    Assert-True ($fake.FolderCalls[0] -ceq '\Nested' -and $fake.Folder.Flags[0] -eq 1) 'exact parent and hidden flag'
    Assert-True ($fake.Folder.Deleted.Count -eq 0) 'observe never deletes'
}
Test-Case 'present observation keeps raw result and roundtrip time' {
    $fake = New-FakeService -Paths @('\Target')
    $r = Invoke-ExactWindowsTask '\target' -ServiceFactory { $fake }
    Assert-True ($r.status -eq 'present' -and $r.verified_absent -eq $false -and $r.exit_code -eq 0) 'case-insensitive exact Windows identity'
    Assert-True ($r.task.last_task_result -eq 267009 -and $r.task.last_run_time -ceq '2026-11-12T02:03:04.1234567') 'result and time preserved'
    Assert-True ($fake.Folder.Deleted.Count -eq 0) 'default observation nonmutating'
}
Test-Case 'running target refuses deletion' {
    $fake = New-FakeService -Paths @('\Target') -State 4
    $r = Invoke-ExactWindowsTask '\Target' Delete -ServiceFactory { $fake }
    Assert-True ($r.status -eq 'present' -and $r.verified_absent -eq $false -and $r.exit_code -eq 2) 'running target remains present'
    Assert-True ($fake.Folder.Deleted.Count -eq 0 -and $r.error.stage -eq 'delete_precondition') 'running target never deleted'
}
Test-Case 'queued target refuses deletion' {
    $fake = New-FakeService -Paths @('\Target') -State 2
    $r = Invoke-ExactWindowsTask '\Target' Delete -ServiceFactory { $fake }
    Assert-True ($r.status -eq 'present' -and $r.verified_absent -eq $false -and $r.exit_code -eq 2) 'queued target remains present'
    Assert-True ($fake.Folder.Deleted.Count -eq 0 -and $r.error.stage -eq 'delete_precondition') 'queued target never deleted'
}
Test-Case 'unknown null missing and invalid state never authorize deletion' {
    foreach ($state in @(0, $null, -1, 5, 'invalid')) {
        $fake = New-FakeService -Paths @('\Target')
        $fake.Folder.Tasks[0].State = $state
        $r = Invoke-ExactWindowsTask '\Target' Delete -ServiceFactory { $fake }
        Assert-True ($r.status -eq 'unknown' -and $null -eq $r.verified_absent -and $r.exit_code -eq 1) 'unknown state is unknown'
        Assert-True ($fake.Folder.Deleted.Count -eq 0) 'unknown state never deleted'
    }
    $fake = New-FakeService -Paths @('\Target')
    $fake.Folder.Tasks[0].PSObject.Properties.Remove('State')
    $r = Invoke-ExactWindowsTask '\Target' Delete -ServiceFactory { $fake }
    Assert-True ($r.status -eq 'unknown' -and $fake.Folder.Deleted.Count -eq 0) 'missing state never deleted'
}
Test-Case 'delete targets only exact name and reenumerates' {
    $fake = New-FakeService -Paths @('\Nested\Target', '\Nested\TargetOther', '\Nested\Other')
    $r = Invoke-ExactWindowsTask '\Nested\Target' Delete -ServiceFactory { $fake }
    Assert-True ($r.verified_absent -eq $true -and $r.delete_returned -and $r.exit_code -eq 0) 'verified deletion'
    Assert-True ($fake.Folder.Deleted.Count -eq 1 -and $fake.Folder.Deleted[0] -ceq 'Target') 'exact delete call'
    Assert-True ($fake.Folder.Tasks.Count -eq 2 -and $fake.Folder.EnumerationCalls -eq 2) 'unrelated tasks preserved and absence rechecked'
    Assert-True ($r.task.last_task_result -eq 267009) 'pre-deletion metadata kept'
}
Test-Case 'post-deletion enumeration failure is unknown' {
    $fake = New-FakeService -Paths @('\Target') -EnumerationDeniedAt 2
    $r = Invoke-ExactWindowsTask '\Target' Delete -ServiceFactory { $fake }
    Assert-True ($r.status -eq 'unknown' -and $null -eq $r.verified_absent -and $r.exit_code -eq 1) 'no fabricated verification'
    Assert-True ($r.delete_returned -and $r.error.stage -eq 'enumerate_after') 'retain delete versus verification distinction'
}
Test-Case 'delete failure is unknown' {
    $fake = New-FakeService -Paths @('\Target') -DeleteDenied
    $r = Invoke-ExactWindowsTask '\Target' Delete -ServiceFactory { $fake }
    Assert-True ($r.status -eq 'unknown' -and $null -eq $r.verified_absent -and $r.error.stage -eq 'delete') 'deletion denied not absence'
}
Test-Case 'delete returning without removing target fails verification' {
    $fake = New-FakeService -Paths @('\Target') -DeleteNoOp
    $r = Invoke-ExactWindowsTask '\Target' Delete -ServiceFactory { $fake }
    Assert-True ($r.status -eq 'unknown' -and $null -eq $r.verified_absent -and $r.exit_code -eq 1) 'remaining task not verified absent'
}
Test-Case 'invalid paths never create service' {
    foreach ($path in @('', '\', 'Target', '\Folder\', '\Target*', '\Tar?get', '\[Target]', '\\Target', '\..\Target', '/Target')) {
        $r = Invoke-ExactWindowsTask $path -ServiceFactory { throw 'must not create service' }
        Assert-True ($r.error.stage -eq 'validate' -and $null -eq $r.verified_absent -and $r.exit_code -eq 1) "reject $path"
    }
}
Test-Case 'invalid action never creates service' {
    $r = Invoke-ExactWindowsTask '\Target' Execute -ServiceFactory { throw 'must not create service' }
    Assert-True ($r.error.stage -eq 'validate' -and $r.exit_code -eq 1) 'reject arbitrary operations'
}
Test-Case 'empty enumeration verifies absence without deletion' {
    $fake = New-FakeService
    $r = Invoke-ExactWindowsTask '\Target' Delete -ServiceFactory { $fake }
    Assert-True ($r.verified_absent -eq $true -and $r.enumeration_count_before -eq 0 -and $r.exit_code -eq 0) 'empty successful enumeration'
    Assert-True (-not $r.delete_attempted -and $fake.Folder.Deleted.Count -eq 0) 'already absent delete is nonmutating'
}
Test-Case 'CLI invalid root returns JSON and nonzero exit without COM access' {
    $executable = Join-Path $PSHOME 'powershell.exe'
    if ($PSVersionTable.PSVersion.Major -ge 6) { $executable = Join-Path $PSHOME 'pwsh.exe' }
    $output = & $executable -NoProfile -ExecutionPolicy Bypass -File $HelperPath -TaskPath '\'
    $exitCode = $LASTEXITCODE
    $r = ($output -join "`n") | ConvertFrom-Json
    Assert-True ($exitCode -eq 1 -and $r.exit_code -eq 1 -and $r.error.stage -eq 'validate') 'CLI structured failure and process exit code'
    $command = Get-Command $HelperPath
    Assert-True (-not $command.Parameters.ContainsKey('ServiceFactory')) 'production CLI cannot inject a mock'
}
Test-Case 'dot source does not change caller error policy or create CLI result' {
    $prior = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = . $HelperPath
        Assert-True ($ErrorActionPreference -eq 'Continue' -and $null -eq $output) 'dot source only declares functions'
    } finally { $ErrorActionPreference = $prior }
}

[ordered]@{ test_kind = 'mock_only'; powershell = $PSVersionTable.PSVersion.ToString(); passed = $passed.Count; cases = $passed } | ConvertTo-Json -Depth 4
