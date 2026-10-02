<#
.SYNOPSIS
    Public E2E entry point for Windows.

.DESCRIPTION
    A thin wrapper. It does not parse the acceptance CLI: arguments are
    forwarded verbatim to the single parser inside the runner container
    (ops/a8/e2e/cli.py), so the documented examples cannot drift from the
    implementation.

    Four host-side jobs that cannot be done from inside the container:

      1. resolve the runner image tag to an immutable image id and digest and
         pass them in, so the run records the identity it actually used;
      2. translate host paths into container paths and bind them;
      3. bridge a local Git repository -- including a Git worktree, whose .git
         is a file containing "gitdir: C:\..." that means nothing inside the
         container -- into a self-contained bare repository holding exactly the
         requested commit;
      4. check every exit code.

    A remote-only run needs nothing on the host but Docker Desktop: no Git, no
    Python, no Java, no Rust, no Go, and no manual WSL preparation.

.EXAMPLE
    .\ops\e2e\Run-E2E.ps1 list

.EXAMPLE
    .\ops\e2e\Run-E2E.ps1 plan `
      --gonka-repo https://github.com/gonka-ai/gonka `
      --gonka-sha <FULL_40_HEX_SHA> `
      --contracts-repo https://github.com/anikiyevichm/gonka24-forward `
      --contracts-sha <FULL_40_HEX_SHA> `
      --profile all `
      --output .\out\plan

.EXAMPLE
    .\ops\e2e\Run-E2E.ps1 run --from .\out\plan\run.lock.json
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ContainerArgs = @()
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
$ComposeFile = Join-Path $RepoRoot 'ops\a8\compose.yaml'
$Service = 'e2e-runner'

function Fail {
    param([string]$Message, [int]$Code = 2)
    Write-Error $Message -ErrorAction Continue
    exit $Code
}

function Assert-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Fail "'$Name' is required on the host but was not found."
    }
}

function Invoke-Checked {
    <#
        Runs a native command, captures stdout, and fails loudly on a non-zero
        exit code. Every host Git and Docker call goes through here so that no
        failure can be silently ignored.
    #>
    param(
        [Parameter(Mandatory)][string]$File,
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$What = 'host command',
        [switch]$AllowFailure
    )
    $output = & $File @Arguments 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0 -and -not $AllowFailure) {
        Fail "$What failed with exit code ${code}:`n$($output -join [Environment]::NewLine)" 1
    }
    return [pscustomobject]@{ ExitCode = $code; Output = ($output -join [Environment]::NewLine).Trim() }
}

function Resolve-HostPath {
    # Handles paths with spaces and paths that do not exist yet.
    param([Parameter(Mandatory)][string]$Path)
    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($resolved) { return $resolved.Path }
    $parent = Split-Path -Parent $Path
    if ([string]::IsNullOrWhiteSpace($parent)) { $parent = '.' }
    $parentResolved = Resolve-Path -LiteralPath $parent -ErrorAction SilentlyContinue
    if (-not $parentResolved) { Fail "Directory does not exist: $parent" }
    return (Join-Path $parentResolved.Path (Split-Path -Leaf $Path))
}

function Get-FlagValue {
    # Reads a flag value without consuming it.
    param([string[]]$Tokens, [string]$Flag)
    for ($i = 0; $i -lt $Tokens.Count - 1; $i++) {
        if ($Tokens[$i] -eq $Flag) { return $Tokens[$i + 1] }
    }
    return $null
}

$script:BridgeSession = $null
$script:BridgeRoot = $null

function Initialize-BridgeSession {
    if ($script:BridgeSession) { return }
    $bridgeRoot = if ($env:E2E_BRIDGE_DIR) { $env:E2E_BRIDGE_DIR } else { Join-Path $RepoRoot '.e2e-bridge' }
    New-Item -ItemType Directory -Path $bridgeRoot -Force | Out-Null
    $rootItem = Get-Item -LiteralPath $bridgeRoot -Force
    if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        Fail "Bridge root is a symlink or reparse point: $bridgeRoot"
    }
    $script:BridgeRoot = [IO.Path]::GetFullPath($rootItem.FullName).TrimEnd('\', '/')
    $session = Join-Path $script:BridgeRoot ("run." + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $session -ErrorAction Stop | Out-Null
    $script:BridgeSession = $session
}

function Remove-BridgeSession {
    if (-not $script:BridgeSession) { return }
    $session = [IO.Path]::GetFullPath($script:BridgeSession)
    $parent = [IO.Path]::GetDirectoryName($session).TrimEnd('\', '/')
    if (-not [string]::Equals($parent, $script:BridgeRoot, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Warning "Bridge session is outside its owned root; leaving it untouched: $session"
        return
    }
    if (Test-Path -LiteralPath $session) {
        $item = Get-Item -LiteralPath $session -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            Write-Warning "Bridge session became a symlink; leaving it untouched: $session"
            return
        }
        Remove-Item -LiteralPath $session -Recurse -Force -ErrorAction Stop
    }
}

function New-BridgeRepository {
    <#
        Copies the objects of a local repository into a fresh bare repository
        and records the requested commit under a dedicated ref.

        The caller's repository is only ever read. Its working tree, its HEAD
        and its uncommitted files are untouched and cannot enter the snapshot.
    #>
    param(
        [Parameter(Mandatory)][string]$Role,
        [Parameter(Mandatory)][string]$Source,
        [string]$Sha
    )
    if ([string]::IsNullOrWhiteSpace($Sha)) {
        Fail "--$Role-path requires --$Role-sha with a full 40-hex commit SHA."
    }
    if ($Sha -notmatch '^[0-9a-fA-F]{40}$') {
        Fail "--$Role-sha must be a full 40-hex commit SHA; got '$Sha'."
    }
    Assert-Command git

    Initialize-BridgeSession
    $bridge = Join-Path $script:BridgeSession "$Role.git"

    Invoke-Checked -File 'git' -Arguments @('init', '--bare', '--quiet', '--', $bridge) `
        -What "git init of the host bridge repository for $Role" | Out-Null

    Invoke-Checked -File 'git' -Arguments @(
        '-C', $bridge, 'fetch', '--no-tags', '--quiet', '--', $Source,
        '+refs/heads/*:refs/e2e/heads/*', '+refs/tags/*:refs/e2e/tags/*'
    ) -What "reading Git objects from '$Source'" | Out-Null

    # A commit reachable only from a detached worktree HEAD still has to work.
    # A failure here is not fatal on its own; the object-type check decides.
    Invoke-Checked -File 'git' -Arguments @('-C', $bridge, 'fetch', '--no-tags', '--quiet', '--', $Source, $Sha) `
        -What 'direct object fetch' -AllowFailure | Out-Null

    $typeResult = Invoke-Checked -File 'git' -Arguments @('-C', $bridge, 'cat-file', '-t', $Sha) `
        -What "looking up $Sha" -AllowFailure
    if ($typeResult.ExitCode -ne 0) {
        Fail "Commit $Sha was not found in '$Source'. No other revision is substituted." 1
    }
    if ($typeResult.Output -ne 'commit') {
        Fail "$Sha in '$Source' is a $($typeResult.Output), not a commit." 1
    }

    Invoke-Checked -File 'git' -Arguments @('-C', $bridge, 'update-ref', "refs/e2e/source/$Role", $Sha) `
        -What "recording $Sha in the host bridge repository" | Out-Null

    return $bridge
}

$exitCode = 1
try {
Assert-Command docker

$gonkaSha = Get-FlagValue -Tokens $ContainerArgs -Flag '--gonka-sha'
$contractsSha = Get-FlagValue -Tokens $ContainerArgs -Flag '--contracts-sha'

$forward = New-Object System.Collections.Generic.List[string]
$runnerImage = if ($env:E2E_RUNNER_IMAGE) { $env:E2E_RUNNER_IMAGE } else { 'a8-runner:local' }
$gonkaBridge = $null
$contractsBridge = $null
$outputHost = $null
$planDirHost = $null
$secretsDirHost = $null
$runArgIndex = -1
$runArgValue = $null
$dockerRootVolume = if ($env:A8_DOCKER_ROOT_VOLUME) { $env:A8_DOCKER_ROOT_VOLUME } else { 'a8-docker-root' }

for ($i = 0; $i -lt $ContainerArgs.Count; $i++) {
    $token = $ContainerArgs[$i]
    switch ($token) {
        '--runner-image' {
            if ($i + 1 -ge $ContainerArgs.Count) { Fail '--runner-image requires a value.' }
            $runnerImage = $ContainerArgs[$i + 1]; $i++
        }
        '--docker-root-volume' {
            if ($i + 1 -ge $ContainerArgs.Count) { Fail '--docker-root-volume requires a value.' }
            $dockerRootVolume = $ContainerArgs[$i + 1]
            if ($dockerRootVolume -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$') {
                Fail "--docker-root-volume must be a plain Docker volume name; got '$dockerRootVolume'."
            }
            $i++
        }
        '--gonka-path' {
            if ($i + 1 -ge $ContainerArgs.Count) { Fail '--gonka-path requires a value.' }
            $gonkaBridge = New-BridgeRepository -Role 'gonka' -Source (Resolve-HostPath $ContainerArgs[$i + 1]) -Sha $gonkaSha
            $forward.Add('--gonka-path'); $forward.Add('/input/gonka'); $i++
        }
        '--contracts-path' {
            if ($i + 1 -ge $ContainerArgs.Count) { Fail '--contracts-path requires a value.' }
            $contractsBridge = New-BridgeRepository -Role 'contracts' -Source (Resolve-HostPath $ContainerArgs[$i + 1]) -Sha $contractsSha
            $forward.Add('--contracts-path'); $forward.Add('/input/contracts'); $i++
        }
        '--output' {
            if ($i + 1 -ge $ContainerArgs.Count) { Fail '--output requires a value.' }
            $outputHost = Resolve-HostPath $ContainerArgs[$i + 1]
            New-Item -ItemType Directory -Path $outputHost -Force | Out-Null
            $forward.Add('--output'); $forward.Add('/out'); $i++
        }
        '--run' {
            # report/recover take a run directory or a bare run id. The value is
            # translated after the loop, because --output may appear later on the
            # command line and it decides what /out is.
            if ($i + 1 -ge $ContainerArgs.Count) { Fail '--run requires a run directory or a run id.' }
            $runArgValue = $ContainerArgs[$i + 1]
            $forward.Add('--run')
            $runArgIndex = $forward.Count
            $forward.Add($runArgValue)
            $i++
        }
        '--from' {
            if ($i + 1 -ge $ContainerArgs.Count) { Fail '--from requires a path to run.lock.json.' }
            $planPath = $ContainerArgs[$i + 1]
            if (-not (Test-Path -LiteralPath $planPath)) { Fail "Plan not found: $planPath" }
            $planPath = (Resolve-Path -LiteralPath $planPath).Path
            if (Test-Path -LiteralPath $planPath -PathType Container) {
                $planDirHost = $planPath
                $forward.Add('--from'); $forward.Add('/input/plan/run.lock.json')
            }
            else {
                $planDirHost = Split-Path -Parent $planPath
                $forward.Add('--from'); $forward.Add("/input/plan/$(Split-Path -Leaf $planPath)")
            }
            $i++
        }
        '--credential-file' {
            if ($i + 1 -ge $ContainerArgs.Count) { Fail '--credential-file requires a value.' }
            $credPath = $ContainerArgs[$i + 1]
            if (-not (Test-Path -LiteralPath $credPath -PathType Leaf)) { Fail "Credential file not found: $credPath" }
            $credPath = (Resolve-Path -LiteralPath $credPath).Path
            $secretsDirHost = Split-Path -Parent $credPath
            $forward.Add('--credential-file'); $forward.Add("/run/secrets/e2e/$(Split-Path -Leaf $credPath)")
            $i++
        }
        default { $forward.Add($token) }
    }
}

# -----------------------------------------------------------------------------
# Translate --run. A bare run id already means the same thing inside the
# container; a host path does not, so it is rewritten to the same location under
# /out. Without --output the output root is inferred from the documented layout
# (<output>\runs\<run-id>), so `report`/`recover` work with nothing but the run
# directory that `run` printed.
# -----------------------------------------------------------------------------
if ($runArgIndex -ge 0) {
    $looksLikePath = $runArgValue -match '[\\/]' -or $runArgValue -eq '.' -or $runArgValue -eq '..'
    if ($looksLikePath) {
        if (-not (Test-Path -LiteralPath $runArgValue -PathType Container)) {
            Fail @"
Run directory not found: $runArgValue
Pass the directory that ``run`` created, or the bare run id together with the --output that holds it.
"@
        }
        $runDirHost = (Resolve-Path -LiteralPath $runArgValue).Path.TrimEnd('\', '/')
        if (-not $outputHost) {
            # Mount the enclosing package, not just the suite directory: the
            # container must be able to read its lock and execution manifests.
            # This only chooses a mount; the container still validates evidence.
            $packageRoot = $runDirHost
            $candidate = $runDirHost
            $markers = @('run.lock.json', 'execution-manifest.json', 'build-manifest.json', 'delivery.json', 'e2e-run-result.json')
            for ($depth = 0; $depth -le 6; $depth++) {
                $found = $false
                foreach ($marker in $markers) {
                    if (Test-Path -LiteralPath (Join-Path $candidate $marker) -PathType Leaf) {
                        $found = $true
                        break
                    }
                }
                if ($found) {
                    $packageRoot = $candidate
                    break
                }
                $parent = Split-Path -Parent $candidate
                if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $candidate) { break }
                $candidate = $parent
            }
            $runParent = Split-Path -Parent $packageRoot
            if ((Split-Path -Leaf $runParent) -eq 'runs') {
                $outputHost = Split-Path -Parent $runParent
            }
            else {
                $outputHost = $runParent
            }
        }
        $outputRoot = $outputHost.TrimEnd('\', '/')
        if ($runDirHost -eq $outputRoot) {
            $forward[$runArgIndex] = '/out'
        }
        elseif ($runDirHost.StartsWith($outputRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            $relative = $runDirHost.Substring($outputRoot.Length + 1).Replace('\', '/')
            $forward[$runArgIndex] = "/out/$relative"
        }
        else {
            Fail @"
--run $runDirHost is outside --output $outputRoot.
Only one host directory is mounted for evidence, so both must live under it.
Either drop --output, or pass the --output that contains this run.
"@
        }
    }
}

$idResult = Invoke-Checked -File 'docker' -Arguments @('image', 'inspect', '--format', '{{.Id}}', $runnerImage) `
    -What "inspecting the runner image '$runnerImage'" -AllowFailure
if ($idResult.ExitCode -ne 0) {
    Fail @"
The runner image '$runnerImage' is not available on this host.
Build it once with:
  .\ops\e2e\Build-Runner.ps1
or pass --runner-image with an image that is present.
"@ 1
}
$imageId = $idResult.Output

$digestResult = Invoke-Checked -File 'docker' `
    -Arguments @('image', 'inspect', '--format', '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}', $imageId) `
    -What 'reading the runner image digest' -AllowFailure
$imageDigest = if ($digestResult.ExitCode -eq 0) { $digestResult.Output } else { '' }

if ($runnerImage -notlike '*@sha256:*') {
    Write-Host "note: runner image locator $runnerImage is a mutable tag; it currently resolves to $imageId"
}
if ([string]::IsNullOrWhiteSpace($imageDigest)) {
    Write-Host 'note: this runner image has no registry digest. Replaying a plan created with it on another host requires transferring the image (docker save / docker load); a rebuild from the same tag produces a different image and is refused.'
}

# Compose must launch the inspected immutable image even if the mutable tag
# moves between image inspect and container creation. Keep the human-selected
# locator inside the container as provenance, as the POSIX wrapper does.
$env:E2E_RUNNER_IMAGE = $imageId
$env:E2E_RUNNER_IMAGE_ID = $imageId
$env:E2E_RUNNER_IMAGE_DIGEST = $imageDigest
$env:GONKA_DIR = if ($gonkaBridge) { $gonkaBridge } else { $RepoRoot }
$env:CONTRACTS_DIR = if ($contractsBridge) { $contractsBridge } else { $RepoRoot }
$env:OUTPUT_DIR = if ($outputHost) { $outputHost } else { Join-Path $RepoRoot 'out' }
$env:E2E_PLAN_DIR = if ($planDirHost) { $planDirHost } else { $RepoRoot }
$env:E2E_SECRETS_DIR = if ($secretsDirHost) { $secretsDirHost } else { $RepoRoot }
$env:A8_DOCKER_ROOT_VOLUME = $dockerRootVolume
New-Item -ItemType Directory -Path $env:OUTPUT_DIR -Force | Out-Null

& docker compose -f $ComposeFile run --rm -e "E2E_RUNNER_IMAGE=$runnerImage" $Service @forward
$exitCode = $LASTEXITCODE
}
finally {
    try { Remove-BridgeSession }
    catch { Write-Warning "Could not clean up the owned bridge session: $_" }
}
exit $exitCode
