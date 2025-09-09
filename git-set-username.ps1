<#
.SYNOPSIS
    Insert a username into an HTTPS Git remote URL.

.DESCRIPTION
    Updates the remote URL of a git repo (default: origin) to include a username.
    If no Path is provided, it uses the current working directory.

.PARAMETER Path
    Path to the repository. Defaults to the current directory.

.PARAMETER Username
    Username to insert into the remote URL.

.PARAMETER RemoteName
    Name of the remote (default: origin).

.PARAMETER All
    Update all remotes instead of just one.
#>

param(
    [string]$Path = (Get-Location).Path,
    [Parameter(Mandatory=$true)][string]$Username,
    [string]$RemoteName = "origin",
    [switch]$All
)

function Throw-IfNoGit {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "Git is not available in PATH. Please install Git or make sure 'git' is reachable."
    }
}

function Enter-Repo {
    param($p)
    if (-not (Test-Path $p)) { throw "Path '$p' does not exist." }
    Push-Location $p
    try {
        if (-not (Test-Path (Join-Path $p ".git")) -and -not (git rev-parse --is-inside-work-tree 2>$null)) {
            throw "'$p' is not a git repository."
        }
    } catch {
        Pop-Location
        throw $_
    }
}

function Insert-Username-Into-Url {
    param($url, $username)

    if ($url -match '^(https?:\/\/)(?:([^@\/]+)@)?(.+)$') {
        $protocol = $matches[1]
        $rest = $matches[3]
        return "$protocol$username@$rest"
    } else {
        return $null
    }
}

try {
    Throw-IfNoGit
    Enter-Repo -p $Path

    $remotes = if ($All) { git remote } else { @($RemoteName) }

    foreach ($r in $remotes) {
        $oldUrl = git remote get-url $r 2>$null
        if (-not $oldUrl) { continue }

        $newUrl = Insert-Username-Into-Url -url $oldUrl -username $Username
        if ($newUrl -and $newUrl -ne $oldUrl) {
            git remote set-url $r $newUrl
            Write-Host "Updated $r:`n  Old: $oldUrl`n  New: $newUrl"
        }
    }
} finally {
    Pop-Location -ErrorAction SilentlyContinue
}
