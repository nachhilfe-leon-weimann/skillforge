<#
.SYNOPSIS
    Grants SkillForge's Entra app read access to the calendar of exactly one Exchange Online mailbox
    through RBAC for Applications, and checks the result.

.DESCRIPTION
    Idempotent: every object is looked up first and reused if it exists and matches this setup;
    an object with the same name that does not match stops the script.

      1. Service principal pointer in Exchange for the Entra app (New-ServicePrincipal).
      2. Management scope whose filter matches exactly one mailbox, by its ExternalDirectoryObjectId
         (the Entra object ID of the mailbox user: unique and stable, unlike an address, which the
         filterable-properties doc says also matches proxy addresses). If Exchange rejects that
         filter, -FilterProperty EmailAddresses scopes by the mailbox's primary SMTP address instead.
         A reused scope must carry exactly this filter.
      3. Role assignment "Application Calendars.Read" for the app, restricted to that scope. A reused
         assignment must be assigned to this app's service principal and use this scope.
      4. Test-ServicePrincipalAuthorization: the in-scope mailbox must be InScope True; the
         out-of-scope mailbox must show at least one "Application Calendars.Read" row, all InScope False.

    Output names only role, scope and True/False - no IDs, tenant data or addresses - so it can be
    pasted into an agent chat. Error messages from Exchange cmdlets may still contain IDs.
    Exit code 0 when the checks hold (or under -WhatIf, when steps would still run first), 1 otherwise:
    a declined step, an object that stays invisible after creation, or a failed check.

    PREREQUISITES
      - PowerShell 7 and the ExchangeOnlineManagement module 3.x or later.
      - Connect-ExchangeOnline run by the maintainer in the same session before this script, as an
        Exchange Administrator (or Organization Management member). The script never signs in itself.
      - The Entra app registration "skillforge" and its Enterprise application (service principal).
        -AppId is the Application (client) ID, -ServicePrincipalObjectId the Object ID shown on the
        ENTERPRISE APPLICATION's overview - not the Object ID on the App registration page.

    WARNING - NEVER GRANT Calendars.* OR Mail.* APPLICATION PERMISSIONS IN ENTRA
      Entra grants and Exchange RBAC grants are a union. An application permission such as
      Calendars.Read consented in Entra ("API permissions") opens every mailbox in the tenant and
      voids this scope. The app needs no Graph application permission in Entra at all. This script
      cannot see Entra grants, and Test-ServicePrincipalAuthorization ignores them.

    PROPAGATION
      New objects can take a moment to become visible. The lookup retries cover only the checks
      (and the removal checks): an error of any Exchange call stops the run (each passes
      -ErrorAction Stop); a rerun fixes a replication error, since the script reuses what exists.
      Test-ServicePrincipalAuthorization bypasses the permission cache, so it passes at once. Real
      Graph calls see a change only after the cache resets: 30 minutes for an idle app, up to 2 hours
      for an active one.

    UNDO
      Run with -Remove (-Mailbox optional; with it, the scope's filter must name that mailbox). It
      first checks that the role assignment and the scope are the ones this setup creates, then
      removes both and fails if either is still there. The service principal pointer stays: it grants nothing on its own, and other
      SkillForge features may hold role assignments through it. Remove it by hand with
      Remove-ServicePrincipal once no role assignment uses it.

.PARAMETER AppId
    Application (client) ID of the Entra app.
.PARAMETER ServicePrincipalObjectId
    Object ID of the Enterprise application (service principal) - not of the App registration.
.PARAMETER Mailbox
    The one mailbox whose calendar the app may read (any identity Get-Mailbox accepts).
.PARAMETER OutOfScopeMailbox
    A second mailbox for the negative check; it must come back InScope False.
.PARAMETER ScopeName
    Name of the management scope; the role assignment is named "<ScopeName>-calendars-read".
.PARAMETER DisplayName
    Display name of the service principal pointer in Exchange.
.PARAMETER FilterProperty
    Recipient property the scope filters on: ExternalDirectoryObjectId (default) or EmailAddresses.
.PARAMETER Remove
    Tear down the role assignment and the scope this script creates instead of creating them.

.EXAMPLE
    Connect-ExchangeOnline -UserPrincipalName <admin-upn>
    ./scripts/graph/exchange-rbac-calendar.ps1 -AppId <app-id> -ServicePrincipalObjectId <sp-object-id> `
        -Mailbox <calendar-mailbox> -OutOfScopeMailbox <other-mailbox> -WhatIf

.EXAMPLE
    ./scripts/graph/exchange-rbac-calendar.ps1 -AppId <app-id> -ServicePrincipalObjectId <sp-object-id> -Remove -WhatIf

.LINK
    https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac
#>
#Requires -Version 7.0
[CmdletBinding(SupportsShouldProcess, DefaultParameterSetName = 'Setup')]
param(
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][guid] $AppId,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][guid] $ServicePrincipalObjectId,
    [Parameter(Mandatory, ParameterSetName = 'Setup')]
    [Parameter(ParameterSetName = 'Remove')]
    [ValidateNotNullOrEmpty()][string] $Mailbox,
    [Parameter(Mandatory, ParameterSetName = 'Setup')][ValidateNotNullOrEmpty()][string] $OutOfScopeMailbox,
    [ValidateLength(1, 47)][ValidatePattern('^[A-Za-z0-9-]+$')][string] $ScopeName = 'skillforge-calendar-lessons',
    [ValidateNotNullOrEmpty()][string] $DisplayName = 'skillforge',
    [ValidateSet('ExternalDirectoryObjectId', 'EmailAddresses')][string] $FilterProperty = 'ExternalDirectoryObjectId',
    [Parameter(Mandatory, ParameterSetName = 'Remove')][switch] $Remove
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# The Exchange cmdlets are functions of a generated module and run in its session state, which this preference
# does not reach: their errors stay non-terminating. Every Exchange call therefore passes -ErrorAction Stop, so a
# failed write never reads as done and a failed read never as "not present".

$RoleName = 'Application Calendars.Read'
$AssignmentName = "$ScopeName-calendars-read"
$LookupAttempts = 6
$LookupDelaySeconds = 10
$appIdText = $AppId.ToString()
$spObjectIdText = $ServicePrincipalObjectId.ToString()

function Fail([string] $Message) {
    Write-Host "FAIL: $Message" -ForegroundColor Red
    exit 1
}

# --- Preconditions -------------------------------------------------------------------------------

$module = Get-Module -Name ExchangeOnlineManagement
if (-not $module) {
    $module = Get-Module -ListAvailable -Name ExchangeOnlineManagement | Sort-Object Version -Descending | Select-Object -First 1
}
if (-not $module -or $module.Version.Major -lt 3) {
    Fail 'ExchangeOnlineManagement 3.x or later is required (Install-Module ExchangeOnlineManagement).'
}
if (-not (Get-Command Get-ConnectionInformation -ErrorAction SilentlyContinue) -or
    -not (Get-ConnectionInformation -ErrorAction Stop | Where-Object { $_.State -eq 'Connected' })) {
    Fail 'Not connected. Run Connect-ExchangeOnline first, then this script in the same session.'
}

# Exact lookups only: a non-existent -Identity makes some Get-* cmdlets return every object.
function Find-ServicePrincipal { Get-ServicePrincipal -ErrorAction Stop | Where-Object { $_.AppId -eq $appIdText } }
function Find-Scope { Get-ManagementScope -ErrorAction Stop | Where-Object { $_.Name -eq $ScopeName } }
function Find-Assignment {
    Get-ManagementRoleAssignment -Role $RoleName -ErrorAction Stop | Where-Object { $_.Name -eq $AssignmentName }
}

function Invoke-ReadOnly([scriptblock] $Command) {
    # Test-ServicePrincipalAuthorization only reads; keep -WhatIf and -Confirm from leaking into it.
    $WhatIfPreference = $false
    $ConfirmPreference = 'None'
    & $Command
}

function Get-OptionalProperty($Object, [string] $Name) {
    # StrictMode throws on a missing property; an assignment may lack one of the assignee fields.
    $property = $Object.PSObject.Properties[$Name]
    if ($property) { "$($property.Value)" }
}

function Get-CalendarRows([string] $Resource) {
    Invoke-ReadOnly { Test-ServicePrincipalAuthorization -Identity $spObjectIdText -Resource $Resource -ErrorAction Stop } |
        Where-Object { $_.RoleName -eq $RoleName }
}

function Format-Row($Row) {
    $scope = if ($Row.AllowedResourceScope -eq $ScopeName) { $ScopeName } else { "<other $($Row.ScopeType)>" }
    "$($Row.RoleName) | $scope | InScope $($Row.InScope)"
}

function Format-Filter([string] $Property, [string] $Value) {
    "$Property -eq '$($Value -replace "'", "''")'"
}

function ConvertTo-NormalFilter([string] $Filter) {
    # Whitespace and outer parentheses are all Exchange may add; case is ignored by -eq.
    $text = ($Filter -replace '\s+', ' ').Trim()
    while ($text -match '^\((.*)\)$') { $text = $Matches[1].Trim() }
    $text
}

function Test-OwnScope($Scope) {
    # With a mailbox: exactly the filter this script writes. Without (-Remove): one clause of that shape.
    $actual = ConvertTo-NormalFilter $Scope.RecipientFilter
    if ($expectedFilter) { return $actual -eq $expectedFilter }
    $actual -match "^(ExternalDirectoryObjectId|EmailAddresses) -eq '([^']|'')*'$"
}

function Test-OwnAssignment($Assignment) {
    # The named assignment must be assigned to this service principal and use this scope, and this app must
    # hold the role through this scope.
    if (-not $servicePrincipal -or "$($Assignment.CustomResourceScope)" -ne $ScopeName) { return $false }
    if ((Get-OptionalProperty $Assignment 'RoleAssigneeType') -ne 'ServicePrincipal') { return $false }
    $assignees = @('RoleAssignee', 'RoleAssigneeName' | ForEach-Object { Get-OptionalProperty $Assignment $_ })
    if ($assignees -notcontains $spObjectIdText) { return $false }
    $rows = @(Invoke-ReadOnly { Test-ServicePrincipalAuthorization -Identity $spObjectIdText -ErrorAction Stop })
    [bool] ($rows | Where-Object { $_.RoleName -eq $RoleName -and $_.AllowedResourceScope -eq $ScopeName })
}

function Wait-Until([string] $Waiting, [string] $Failure, [scriptblock] $Condition) {
    # A change can take a moment to replicate; look again a few times before giving up.
    for ($attempt = 1; $attempt -le $LookupAttempts; $attempt++) {
        if (& $Condition) { return }
        if ($attempt -lt $LookupAttempts) {
            Write-Host "$Waiting; looking again in $LookupDelaySeconds s ($attempt/$($LookupAttempts - 1))."
            Start-Sleep -Seconds $LookupDelaySeconds
        }
    }
    Fail $Failure
}

function Wait-Visible([string] $What, [scriptblock] $Lookup) {
    Wait-Until "$What not visible yet" "$What is still not visible. Run the script again in a few minutes; it reuses what exists." { [bool] (& $Lookup) }
}

function Wait-Gone([string] $What, [scriptblock] $Lookup) {
    Wait-Until "$What still there" "$What is still there after its removal; remove it by hand." { -not (& $Lookup) }
}

$servicePrincipal = Find-ServicePrincipal
if ($servicePrincipal -and $servicePrincipal.ObjectId -ne $spObjectIdText) {
    Fail 'An Exchange service principal for this AppId exists with a different ObjectId. Check that -ServicePrincipalObjectId is the Enterprise application Object ID.'
}

$expectedFilter = $null
if ($Mailbox) {
    $target = Get-Mailbox -Identity $Mailbox -ErrorAction Stop
    $targetObjectId = [string] $target.ExternalDirectoryObjectId
    if (-not $targetObjectId) { Fail 'The mailbox has no ExternalDirectoryObjectId; it cannot be scoped this way.' }
    $filterValue = if ($FilterProperty -eq 'EmailAddresses') { [string] $target.PrimarySmtpAddress } else { $targetObjectId }
    $expectedFilter = Format-Filter $FilterProperty $filterValue
}

# --- Remove --------------------------------------------------------------------------------------

if ($Remove) {
    # Check both objects before touching either: a foreign object with the same name stays.
    $assignment = Find-Assignment
    if ($assignment -and -not (Test-OwnAssignment $assignment)) {
        Fail "Role assignment $AssignmentName exists but does not give this app $RoleName on $ScopeName. Nothing removed; check it by hand."
    }
    $scope = Find-Scope
    if ($scope -and -not (Test-OwnScope $scope)) {
        Fail "Management scope $ScopeName has a filter this setup does not write$(if ($Mailbox) { ' for -Mailbox' }). Nothing removed; check it by hand."
    }

    $declined = 0
    if (-not $assignment) {
        Write-Host "Role assignment ${AssignmentName}: not present."
    } elseif ($PSCmdlet.ShouldProcess("role assignment $AssignmentName", 'Remove')) {
        # Confirmed above; Exchange's own prompt would let a "No" leave the grant behind a "removed".
        $assignment | Remove-ManagementRoleAssignment -Confirm:$false -ErrorAction Stop
        Wait-Gone "Role assignment $AssignmentName" { Find-Assignment }
        Write-Host "Role assignment $AssignmentName removed."
    } else { $declined++ }

    if (-not $scope) {
        Write-Host "Management scope ${ScopeName}: not present."
    } elseif ($declined -and -not $WhatIfPreference) {
        Write-Host "Management scope $ScopeName kept: its role assignment still uses it."
    } elseif ($PSCmdlet.ShouldProcess("management scope $ScopeName", 'Remove')) {
        $scope | Remove-ManagementScope -Confirm:$false -ErrorAction Stop
        Wait-Gone "Management scope $ScopeName" { Find-Scope }
        Write-Host "Management scope $ScopeName removed."
    } else { $declined++ }

    Write-Host 'Service principal pointer kept: it grants nothing on its own. Remove it with Remove-ServicePrincipal once no role assignment uses it.'
    if ($declined -and -not $WhatIfPreference) { Fail "$declined step(s) declined; not everything was removed." }
    Write-Host 'Done. Graph calls may keep working until the cache resets (30 minutes to 2 hours).'
    exit 0
}

# --- Create --------------------------------------------------------------------------------------

$matched = @(Get-Recipient -RecipientPreviewFilter $expectedFilter -ResultSize 2 -ErrorAction Stop)
if ($matched.Count -ne 1) { Fail "The scope filter matches $($matched.Count) recipients, expected exactly 1." }
$other = Get-Mailbox -Identity $OutOfScopeMailbox -ErrorAction Stop
if ($other.ExternalDirectoryObjectId -eq $targetObjectId) { Fail '-OutOfScopeMailbox is the same mailbox as -Mailbox.' }

# Check what exists before creating anything: a same-named object that is not this setup's stops the run.
$scope = Find-Scope
if ($scope -and -not (Test-OwnScope $scope)) {
    Fail "Management scope $ScopeName exists with another filter (another mailbox, -FilterProperty or a wider filter). Use another -ScopeName or run with -Remove first."
}
$assignment = Find-Assignment
if ($assignment -and -not (Test-OwnAssignment $assignment)) {
    Fail "Role assignment $AssignmentName exists but does not give this app $RoleName on $ScopeName. Check it by hand."
}

$declined = 0  # ShouldProcess said no: -WhatIf, or a step declined at -Confirm

# 1. Service principal pointer
if ($servicePrincipal) {
    Write-Host 'Service principal pointer: exists, reused.'
} elseif ($PSCmdlet.ShouldProcess("service principal pointer $DisplayName", 'Create')) {
    $servicePrincipal = New-ServicePrincipal -AppId $appIdText -ObjectId $spObjectIdText -DisplayName $DisplayName -ErrorAction Stop
    Write-Host 'Service principal pointer: created.'
} else { $declined++ }

# 2. Management scope for exactly one mailbox
if ($scope) {
    Write-Host "Management scope ${ScopeName}: exists, reused."
} elseif ($PSCmdlet.ShouldProcess("management scope $ScopeName", 'Create')) {
    $null = New-ManagementScope -Name $ScopeName -RecipientRestrictionFilter $expectedFilter -ErrorAction Stop
    Write-Host "Management scope ${ScopeName}: created."
} else { $declined++ }

# 3. Role assignment, restricted to the scope
if ($assignment) {
    Write-Host "Role assignment ${AssignmentName}: exists, reused."
} elseif ($PSCmdlet.ShouldProcess("role assignment $AssignmentName ($RoleName on $ScopeName)", 'Create')) {
    $null = New-ManagementRoleAssignment -Name $AssignmentName -Role $RoleName -App $spObjectIdText -CustomResourceScope $ScopeName -ErrorAction Stop
    Write-Host "Role assignment ${AssignmentName}: created."
} else { $declined++ }

if ($declined) {
    if ($WhatIfPreference) {
        Write-Host "Checks skipped (-WhatIf): $declined step(s) would run first."
        exit 0
    }
    Fail "$declined step(s) declined; nothing was checked. Run again and confirm every step."
}

# 4. Checks (bypass the permission cache)
Wait-Visible 'The service principal pointer' { Find-ServicePrincipal }
Wait-Visible "Management scope $ScopeName" { Find-Scope }
Wait-Visible "Role assignment $AssignmentName" { Find-Assignment }

$ok = $true
Write-Host '--- Test-ServicePrincipalAuthorization ---'

$inRows = @(Get-CalendarRows $target.DistinguishedName)
$inRows | ForEach-Object { Write-Host ("in-scope mailbox:     " + (Format-Row $_)) }
$hit = $inRows | Where-Object { $_.AllowedResourceScope -eq $ScopeName -and "$($_.InScope)" -eq 'True' }
if (-not $hit) { $ok = $false; Write-Host "FAIL: $RoleName is not InScope True for the mailbox via $ScopeName." -ForegroundColor Red }
if ($inRows | Where-Object { $_.AllowedResourceScope -ne $ScopeName }) {
    $ok = $false; Write-Host "FAIL: the app holds $RoleName through another scope as well; remove it." -ForegroundColor Red
}

$outRows = @(Get-CalendarRows $other.DistinguishedName)
$outRows | ForEach-Object { Write-Host ("out-of-scope mailbox: " + (Format-Row $_)) }
if (-not $outRows) {
    $ok = $false; Write-Host "FAIL: no $RoleName row for the out-of-scope mailbox; the negative check proves nothing." -ForegroundColor Red
} elseif ($outRows | Where-Object { "$($_.InScope)" -ne 'False' }) {
    $ok = $false; Write-Host "FAIL: $RoleName reaches the out-of-scope mailbox." -ForegroundColor Red
}

if (-not $ok) { exit 1 }
Write-Host 'OK. Graph calls work once the cache resets: 30 minutes (idle app) to 2 hours (active app).' -ForegroundColor Green
Write-Host 'Reminder: Entra "API permissions" of the app must hold no Calendars.* or Mail.* application permission.'
exit 0
