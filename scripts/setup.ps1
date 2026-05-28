# One-time environment setup for legal-deal-sourcing on Windows.
#
# What it does:
#   1. Installs the `uv` Python tool manager if not already present.
#   2. Creates a project-local virtualenv via `uv` and installs deps from
#      pyproject.toml (including the [dev] extras).
#   3. Copies `.env.example` -> `.env` if `.env` does not exist.
#
# What it does NOT do:
#   * Run Alembic. The first migration is generated and reviewed
#     separately (see docs/runbook.md or the M2 check-in).
#   * Modify any global PATH beyond what the uv installer adds.
#
# Run from the project root:
#     powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
# or, if your execution policy allows it:
#     .\scripts\setup.ps1

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

# 0. Sanity: are we in the project root?
if (-not (Test-Path ".\pyproject.toml")) {
    throw "Run this script from the project root (where pyproject.toml lives)."
}

# 1. Install uv if missing.
Write-Step "Checking for uv"
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Host "uv not found. Installing from astral.sh..." -ForegroundColor Yellow
    # Official installer: https://docs.astral.sh/uv/getting-started/installation/
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    # The installer adds ~\.local\bin to PATH for new shells.
    # Pull it into this shell so the next step finds uv.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    $uv = Get-Command uv -ErrorAction Stop
    Write-Host "Installed uv at $($uv.Source)" -ForegroundColor Green
} else {
    Write-Host "uv already installed at $($uv.Source)" -ForegroundColor Green
}
& uv --version

# 2. Sync deps (creates .venv, installs runtime + dev extras).
Write-Step "Syncing dependencies (uv sync --extra dev)"
& uv sync --extra dev
if ($LASTEXITCODE -ne 0) { throw "uv sync failed." }

# 3. .env from template.
Write-Step "Ensuring .env exists"
if (-not (Test-Path ".\.env")) {
    Copy-Item ".\.env.example" ".\.env"
    Write-Host "Created .env from .env.example. Edit it before running scrapers." -ForegroundColor Green
} else {
    Write-Host ".env already exists - leaving it alone." -ForegroundColor Green
}

Write-Step "Done."
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan

# Use a single-quoted here-string so embedded quotes and parens are taken literally.
$nextSteps = @'
  * Confirm settings load:   uv run python -c "from legal_sourcing.config import get_settings; print(get_settings())"
  * Run tests:               make test    (or  uv run pytest)
  * Generate first migration (after schema review): uv run alembic revision --autogenerate -m "initial schema"
'@
Write-Host $nextSteps