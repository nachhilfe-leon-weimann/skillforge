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
         assignment must give this app the role on this scope.
      4. Test-ServicePrincipalAuthorization: the in-scope mailbox must be InScope True; the
         out-of-scope mailbox must be InScope False for every "Application Calendars.Read" row.

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
      New objects can take a moment to become visible; the script looks them up again a few times
      before it gives up. Test-ServicePrincipalAuthorization bypasses the permission cache, so it
      passes at once. Real Graph calls see a change only after the cache resets: 30 minutes for an
      idle app, up to 2 hours for an active one.

    UNDO
      Run with -Remove (-Mailbox optional; with it, the scope's filter must name that mailbox). It
      first checks that the role assignment and the scope are the ones this setup creates, then
      removes both. The service principal pointer stays: it grants nothing on its own, and other
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
    -not (Get-ConnectionInformation | Where-Object { $_.State -eq 'Connected' })) {
    Fail 'Not connected. Run Connect-ExchangeOnline first, then this script in the same session.'
}

# Exact lookups only: a non-existent -Identity makes some Get-* cmdlets return every object.
function Find-ServicePrincipal { Get-ServicePrincipal | Where-Object { $_.AppId -eq $appIdText } }
function Find-Scope { Get-ManagementScope | Where-Object { $_.Name -eq $ScopeName } }
function Find-Assignment { Get-ManagementRoleAssignment -Role $RoleName | Where-Object { $_.Name -eq $AssignmentName } }

function Invoke-ReadOnly([scriptblock] $Command) {
    # Test-ServicePrincipalAuthorization only reads; keep -WhatIf from leaking into it.
    $WhatIfPreference = $false
    & $Command
}

function Get-CalendarRows([string] $Resource) {
    Invoke-ReadOnly { Test-ServicePrincipalAuthorization -Identity $spObjectIdText -Resource $Resource } |
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
    # The named assignment must use this scope, and this app must hold the role through this scope.
    if (-not $servicePrincipal -or "$($Assignment.CustomResourceScope)" -ne $ScopeName) { return $false }
    $rows = @(Invoke-ReadOnly { Test-ServicePrincipalAuthorization -Identity $spObjectIdText })
    [bool] ($rows | Where-Object { $_.RoleName -eq $RoleName -and $_.AllowedResourceScope -eq $ScopeName })
}

function Wait-Lookup([string] $What, [scriptblock] $Lookup) {
    # A new object can take a moment to replicate; look again a few times before giving up.
    for ($attempt = 1; $attempt -le $LookupAttempts; $attempt++) {
        $found = & $Lookup
        if ($found) { return $found }
        if ($attempt -lt $LookupAttempts) {
            Write-Host "$What not visible yet; looking again in $LookupDelaySeconds s ($attempt/$($LookupAttempts - 1))."
            Start-Sleep -Seconds $LookupDelaySeconds
        }
    }
    Fail "$What is still not visible. Run the script again in a few minutes; it reuses what exists."
}

$servicePrincipal = Find-ServicePrincipal
if ($servicePrincipal -and $servicePrincipal.ObjectId -ne $spObjectIdText) {
    Fail 'An Exchange service principal for this AppId exists with a different ObjectId. Check that -ServicePrincipalObjectId is the Enterprise application Object ID.'
}

$expectedFilter = $null
if ($Mailbox) {
    $target = Get-Mailbox -Identity $Mailbox
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
        $assignment | Remove-ManagementRoleAssignment
        Write-Host "Role assignment $AssignmentName removed."
    } else { $declined++ }

    if (-not $scope) {
        Write-Host "Management scope ${ScopeName}: not present."
    } elseif ($declined -and -not $WhatIfPreference) {
        Write-Host "Management scope $ScopeName kept: its role assignment still uses it."
    } elseif ($PSCmdlet.ShouldProcess("management scope $ScopeName", 'Remove')) {
        $scope | Remove-ManagementScope
        Write-Host "Management scope $ScopeName removed."
    } else { $declined++ }

    Write-Host 'Service principal pointer kept: it grants nothing on its own. Remove it with Remove-ServicePrincipal once no role assignment uses it.'
    if ($declined -and -not $WhatIfPreference) { Fail "$declined step(s) declined; not everything was removed." }
    Write-Host 'Done. Graph calls may keep working until the cache resets (30 minutes to 2 hours).'
    exit 0
}

# --- Create --------------------------------------------------------------------------------------

$matched = @(Get-Recipient -RecipientPreviewFilter $expectedFilter -ResultSize 2)
if ($matched.Count -ne 1) { Fail "The scope filter matches $($matched.Count) recipients, expected exactly 1." }
$other = Get-Mailbox -Identity $OutOfScopeMailbox
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
    $servicePrincipal = New-ServicePrincipal -AppId $appIdText -ObjectId $spObjectIdText -DisplayName $DisplayName
    Write-Host 'Service principal pointer: created.'
} else { $declined++ }

# 2. Management scope for exactly one mailbox
if ($scope) {
    Write-Host "Management scope ${ScopeName}: exists, reused."
} elseif ($PSCmdlet.ShouldProcess("management scope $ScopeName", 'Create')) {
    $null = New-ManagementScope -Name $ScopeName -RecipientRestrictionFilter $expectedFilter
    Write-Host "Management scope ${ScopeName}: created."
} else { $declined++ }

# 3. Role assignment, restricted to the scope
if ($assignment) {
    Write-Host "Role assignment ${AssignmentName}: exists, reused."
} elseif ($PSCmdlet.ShouldProcess("role assignment $AssignmentName ($RoleName on $ScopeName)", 'Create')) {
    $null = New-ManagementRoleAssignment -Name $AssignmentName -Role $RoleName -App $spObjectIdText -CustomResourceScope $ScopeName
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
$null = Wait-Lookup 'The service principal pointer' { Find-ServicePrincipal }
$null = Wait-Lookup "Management scope $ScopeName" { Find-Scope }
$null = Wait-Lookup "Role assignment $AssignmentName" { Find-Assignment }

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
if ($outRows | Where-Object { "$($_.InScope)" -ne 'False' }) {
    $ok = $false; Write-Host "FAIL: $RoleName reaches the out-of-scope mailbox." -ForegroundColor Red
}

if (-not $ok) { exit 1 }
Write-Host 'OK. Graph calls work once the cache resets: 30 minutes (idle app) to 2 hours (active app).' -ForegroundColor Green
Write-Host 'Reminder: Entra "API permissions" of the app must hold no Calendars.* or Mail.* application permission.'
exit 0
