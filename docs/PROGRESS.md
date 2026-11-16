# ReturnProof — build progress

Tracked milestone by milestone. Each row is committed only after `ruff`, `mypy`
and `pytest` pass locally.

| Milestone | Scope | Status | Commit | Tests |
|---|---|---|---|---|
| R-001 | Scaffold, app factory, settings, `/healthz`, tooling, CI | done | 897e980 | 2 |
| R-002 | Domain models, Alembic, synthetic seed | pending | — | — |
| R-003 | Auth, sessions, roles, org isolation, API keys | pending | — | — |
| R-004 | Evidence vault + hash chain + tamper check | done | ccbc42a | 21 |
| R-005 | Product identity verification | done | 3408856 | 30 |
| R-006 | Claim/dispute pack builder | done | 5b1fa93 | 38 |
| R-007 | Settlement import + recovery ledger | done | 9af12ca | 44 |
| R-008 | Web UI | done | 8e74986 | 47 |
| R-009 | Ops: audit, export, jobs, limits, logging | done | 5e1e986 | 56 |
| R-010 | Docs, demo, coverage gate | done | f42ec4e | 57 |

## Final verification

- `ruff check returnproof tests` — clean
- `mypy returnproof` — clean (strict)
- `pytest --cov --cov-fail-under=85` — 57 passed, 87.33% total coverage
- CI matrix: Ubuntu + Windows, Python 3.12, coverage gate 85
- Demo verified locally: sample seed, 8 evidence files, 4 verifications, HTML+PDF claim pack, API-key CLI

No milestone is blocked. All work stayed inside `C:/Users/Admin/projects/returnproof`.
