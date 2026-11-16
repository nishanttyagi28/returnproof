param([Parameter(Position = 0)][string]$Task = "check")

switch ($Task) {
    "install" { python -m pip install -e ".[dev]" }
    "lint"    { ruff check returnproof tests }
    "type"    { mypy returnproof }
    "test"    { pytest }
    "check"   { ruff check returnproof tests; mypy returnproof; pytest }
    "run"     { uvicorn returnproof.app:app --reload --port 8000 }
    "seed"    { python -m returnproof.seed }
    "demo"    { python -m returnproof.demo }
    default   { Write-Host "Unknown task: $Task" }
}
