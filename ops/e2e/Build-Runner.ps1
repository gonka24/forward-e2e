<#
.SYNOPSIS
    One-time bootstrap of the E2E tools container on Windows.

.DESCRIPTION
    Builds ONLY the runner image: the container holding Git, Docker CE, Go,
    Rust, cosmwasm-check, Java and Node, together with the versioned harness,
    catalog and verifier. It does not build, fetch or run anything from a target
    repository, and it never starts the inner Docker daemon.

    After this has run once, an acceptance run is a single command:
      .\ops\e2e\Run-E2E.ps1 run --gonka-repo ... --gonka-sha ... `
                                --contracts-repo ... --contracts-sha ... --profile all

    Nothing beyond Docker Desktop is required on the host: no Python, no Java,
    no Rust, no Go, and no manual WSL preparation.
#>
[CmdletBinding()]
param(
    [string]$Image = $(if ($env:E2E_RUNNER_IMAGE) { $env:E2E_RUNNER_IMAGE } else { 'a8-runner:local' }),
    [string]$RunnerSha = $env:E2E_RUNNER_SHA,
    [string]$RunnerRepo = $(if ($env:E2E_RUNNER_REPO) { $env:E2E_RUNNER_REPO } else { 'https://github.com/gonka24/forward-e2e.git' })
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
$ComposeFile = Join-Path $RepoRoot 'ops\runner\compose.yaml'

function Fail {
    param([string]$Message, [int]$Code = 1)
    Write-Error $Message -ErrorAction Continue
    exit $Code
}

if ($RunnerSha -cnotmatch '^[0-9a-f]{40}$') {
    Fail '-RunnerSha requires a full lowercase 40-hex commit SHA.' 2
}
if (-not $RunnerRepo.StartsWith('https://') -or $RunnerRepo.Contains('#')) {
    Fail '-RunnerRepo requires an HTTPS Git URL without a revision fragment.' 2
}
$env:E2E_RUNNER_SHA = $RunnerSha
$env:E2E_RUNNER_REPO = $RunnerRepo

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail 'Docker is required on the host but was not found.'
}

$env:E2E_RUNNER_IMAGE = $Image
# Only needed so compose can interpolate bind sources; the build never reads them.
if (-not $env:GONKA_DIR) { $env:GONKA_DIR = $RepoRoot }
if (-not $env:CONTRACTS_DIR) { $env:CONTRACTS_DIR = $RepoRoot }
if (-not $env:OUTPUT_DIR) { $env:OUTPUT_DIR = Join-Path $RepoRoot 'out' }
if (-not $env:E2E_PLAN_DIR) { $env:E2E_PLAN_DIR = $RepoRoot }
if (-not $env:E2E_SECRETS_DIR) { $env:E2E_SECRETS_DIR = $RepoRoot }
New-Item -ItemType Directory -Path $env:OUTPUT_DIR -Force | Out-Null

Write-Host "Building the E2E runner image $Image ..."
Write-Host "  runner SHA: $RunnerSha"
& docker compose -f $ComposeFile build e2e-runner
if ($LASTEXITCODE -ne 0) { Fail "Runner image build failed with exit code $LASTEXITCODE." $LASTEXITCODE }

$imageId = (& docker image inspect --format '{{.Id}}' $Image)
if ($LASTEXITCODE -ne 0) { Fail 'The image was built but could not be inspected.' 1 }
$digest = (& docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' $imageId)
if ($LASTEXITCODE -ne 0) { $digest = '' }

Write-Host ''
Write-Host 'Runner image ready.'
Write-Host "  locator : $Image"
Write-Host "  image id: $imageId"
if ([string]::IsNullOrWhiteSpace($digest)) {
    Write-Host '  digest  : (none - local image only)'
    Write-Host '            A plan created with this image can only be replayed on a host that'
    Write-Host '            has this exact image. Transfer it with docker save / docker load;'
    Write-Host '            a rebuild from the same tag produces a different image and is refused.'
}
else {
    Write-Host "  digest  : $digest (retrieval hint; publication not verified)"
}
Write-Host ''
Write-Host 'Next: .\ops\e2e\Run-E2E.ps1 list'
