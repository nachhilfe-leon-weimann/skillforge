<#
.SYNOPSIS
    Grants SkillForge's Entra app read access to the calendar of exactly one Exchange Online mailbox
    through RBAC for Applications, and checks the result.

.DESCRIPTION
    Idempotent: every object is looked up first and reused if it exists.

      1. Service principal pointer in Exchange for the Entra app (New-ServicePrincipal).
      2. Management scope whose filter matches exactly one mailbox, by its ExternalDirectoryObjectId
         (the Entra object ID of the mailbox user: unique and stable, unlike an address, which the
         filterable-properties doc says also matches proxy addresses). If Exchange rejects that
         filter, -FilterProperty EmailAddresses scopes by the mailbox's primary SMTP address instead.
      3. Role assignment "Application Calendars.Read" for the app, restricted to that scope.
      4. Test-ServicePrincipalAuthorization: the in-scope mailbox must be InScope True; the optional
         out-of-scope mailbox must be InScope False for every "Application Calendars.Read" row.

    Output names only role, scope and True/False - no IDs, tenant data or addresses - so it can be
    pasted into an agent chat. Error messages from Exchange cmdlets may still contain IDs.
    Exit code 0 when the checks hold, 1 otherwise.

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
      Test-ServicePrincipalAuthorization bypasses the permission cache, so it passes at once. Real
      Graph calls see a change only after the cache resets: 30 minutes for an idle app, up to
      2 hours for an active one.

    UNDO
      Run again with -Remove (same parameters). It removes the role assignment and the scope this
      script created, and the service principal pointer only if no other role is still assigned to
      it (other SkillForge features get their own assignments on the same app).

.PARAMETER AppId
    Application (client) ID of the Entra app.
.PARAMETER ServicePrincipalObjectId
    Object ID of the Enterprise application (service principal) - not of the App registration.
.PARAMETER Mailbox
    The one mailbox whose calendar the app may read (any identity Get-Mailbox accepts).
.PARAMETER OutOfScopeMailbox
    Optional second mailbox for the negative check; it must come back InScope False.
.PARAMETER ScopeName
    Name of the management scope; the role assignment is named "<ScopeName>-calendars-read".
.PARAMETER DisplayName
    Display name of the service principal pointer in Exchange.
.PARAMETER FilterProperty
    Recipient property the scope filters on: ExternalDirectoryObjectId (default) or EmailAddresses.
.PARAMETER Remove
    Tear down what this script creates instead of creating it.

.EXAMPLE
    Connect-ExchangeOnline -UserPrincipalName <admin-upn>
    ./scripts/graph/exchange-rbac-calendar.ps1 -AppId <app-id> -ServicePrincipalObjectId <sp-object-id> `
        -Mailbox <calendar-mailbox> -OutOfScopeMailbox <other-mailbox> -WhatIf

.LINK
    https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac
#>
#Requires -Version 7.0
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][guid] $AppId,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][guid] $ServicePrincipalObjectId,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string] $Mailbox,
    [ValidateNotNullOrEmpty()][string] $OutOfScopeMailbox,
    [ValidateLength(1, 47)][ValidatePattern('^[A-Za-z0-9-]+$')][string] $ScopeName = 'skillforge-calendar-lessons',
    [ValidateNotNullOrEmpty()][string] $DisplayName = 'skillforge',
    [ValidateSet('ExternalDirectoryObjectId', 'EmailAddresses')][string] $FilterProperty = 'ExternalDirectoryObjectId',
    [switch] $Remove
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RoleName = 'Application Calendars.Read'
$AssignmentName = "$ScopeName-calendars-read"
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

function Get-CalendarRows([string] $Resource) {
    # The test cmdlet only reads; keep -WhatIf from leaking into it.
    & { $WhatIfPreference = $false
        @(Test-ServicePrincipalAuthorization -Identity $spObjectIdText -Resource $Resource |
            Where-Object { $_.RoleName -eq $RoleName }) }
}

function Format-Row($Row) {
    $scope = if ($Row.AllowedResourceScope -eq $ScopeName) { $ScopeName } else { "<other $($Row.ScopeType)>" }
    "$($Row.RoleName) | $scope | InScope $($Row.InScope)"
}

$servicePrincipal = Find-ServicePrincipal
if ($servicePrincipal -and $servicePrincipal.ObjectId -ne $spObjectIdText) {
    Fail 'An Exchange service principal for this AppId exists with a different ObjectId. Check that -ServicePrincipalObjectId is the Enterprise application Object ID.'
}

# --- Remove --------------------------------------------------------------------------------------

if ($Remove) {
    if ($assignment = Find-Assignment) {
        if ($PSCmdlet.ShouldProcess("role assignment $AssignmentName", 'Remove')) {
            $assignment | Remove-ManagementRoleAssignment
            Write-Host "Role assignment $AssignmentName removed."
        }
    } else { Write-Host "Role assignment ${AssignmentName}: not present." }

    if ($scope = Find-Scope) {
        if ($PSCmdlet.ShouldProcess("management scope $ScopeName", 'Remove')) {
            $scope | Remove-ManagementScope
            Write-Host "Management scope $ScopeName removed."
        }
    } else { Write-Host "Management scope ${ScopeName}: not present." }

    if ($servicePrincipal) {
        $remaining = & { $WhatIfPreference = $false; @(Test-ServicePrincipalAuthorization -Identity $spObjectIdText) }
        if ($remaining.Count -gt 0) {
            Write-Host "Service principal pointer kept: $($remaining.Count) other role assignment(s) still use it."
        } elseif ($PSCmdlet.ShouldProcess("service principal pointer $DisplayName", 'Remove')) {
            Remove-ServicePrincipal -Identity $spObjectIdText
            Write-Host 'Service principal pointer removed.'
        }
    } else { Write-Host 'Service principal pointer: not present.' }

    Write-Host 'Done. Graph calls may keep working until the cache resets (30 minutes to 2 hours).'
    exit 0
}

# --- Create --------------------------------------------------------------------------------------

# 1. Service principal pointer
if ($servicePrincipal) {
    Write-Host 'Service principal pointer: exists, reused.'
} elseif ($PSCmdlet.ShouldProcess("service principal pointer $DisplayName", 'Create')) {
    $servicePrincipal = New-ServicePrincipal -AppId $appIdText -ObjectId $spObjectIdText -DisplayName $DisplayName
    Write-Host 'Service principal pointer: created.'
}

# 2. Management scope for exactly one mailbox
$target = Get-Mailbox -Identity $Mailbox
$targetObjectId = [string] $target.ExternalDirectoryObjectId
if (-not $targetObjectId) { Fail 'The mailbox has no ExternalDirectoryObjectId; it cannot be scoped this way.' }
$filterValue = if ($FilterProperty -eq 'EmailAddresses') { [string] $target.PrimarySmtpAddress } else { $targetObjectId }
$filter = "$FilterProperty -eq '$($filterValue -replace "'", "''")'"

$matched = @(Get-Recipient -RecipientPreviewFilter $filter -ResultSize 2)
if ($matched.Count -ne 1) { Fail "The scope filter matches $($matched.Count) recipients, expected exactly 1." }

if ($scope = Find-Scope) {
    if ($scope.RecipientFilter -notmatch [regex]::Escape($filterValue)) {
        Fail "Management scope $ScopeName exists but targets a different mailbox. Use another -ScopeName or run with -Remove first."
    }
    Write-Host "Management scope ${ScopeName}: exists, reused."
} elseif ($PSCmdlet.ShouldProcess("management scope $ScopeName", 'Create')) {
    $null = New-ManagementScope -Name $ScopeName -RecipientRestrictionFilter $filter
    Write-Host "Management scope ${ScopeName}: created."
}

# 3. Role assignment, restricted to the scope
if ($assignment = Find-Assignment) {
    Write-Host "Role assignment ${AssignmentName}: exists, reused."
} elseif ($PSCmdlet.ShouldProcess("role assignment $AssignmentName ($RoleName on $ScopeName)", 'Create')) {
    $null = New-ManagementRoleAssignment -Name $AssignmentName -Role $RoleName -App $spObjectIdText -CustomResourceScope $ScopeName
    Write-Host "Role assignment ${AssignmentName}: created."
}

# 4. Checks (bypass the permission cache)
if (-not (Find-ServicePrincipal) -or -not (Find-Assignment)) {
    Write-Host 'Checks skipped: nothing was created (-WhatIf).'
    exit 0
}

$ok = $true
Write-Host '--- Test-ServicePrincipalAuthorization ---'

$inRows = Get-CalendarRows $target.DistinguishedName
$inRows | ForEach-Object { Write-Host ("in-scope mailbox:     " + (Format-Row $_)) }
$hit = $inRows | Where-Object { $_.AllowedResourceScope -eq $ScopeName -and "$($_.InScope)" -eq 'True' }
if (-not $hit) { $ok = $false; Write-Host "FAIL: $RoleName is not InScope True for the mailbox via $ScopeName." -ForegroundColor Red }
if ($inRows | Where-Object { $_.AllowedResourceScope -ne $ScopeName }) {
    $ok = $false; Write-Host "FAIL: the app holds $RoleName through another scope as well; remove it." -ForegroundColor Red
}

if ($OutOfScopeMailbox) {
    $other = Get-Mailbox -Identity $OutOfScopeMailbox
    if ($other.ExternalDirectoryObjectId -eq $targetObjectId) { Fail '-OutOfScopeMailbox is the same mailbox as -Mailbox.' }
    $outRows = Get-CalendarRows $other.DistinguishedName
    $outRows | ForEach-Object { Write-Host ("out-of-scope mailbox: " + (Format-Row $_)) }
    if ($outRows | Where-Object { "$($_.InScope)" -ne 'False' }) {
        $ok = $false; Write-Host "FAIL: $RoleName reaches the out-of-scope mailbox." -ForegroundColor Red
    }
}

if (-not $ok) { exit 1 }
Write-Host 'OK. Graph calls work once the cache resets: 30 minutes (idle app) to 2 hours (active app).' -ForegroundColor Green
Write-Host 'Reminder: Entra "API permissions" of the app must hold no Calendars.* or Mail.* application permission.'
exit 0
