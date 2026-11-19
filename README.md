# tradoortest

Internal Astral-style strategy trader for one user. Scope is Solana spot, long-only. Phase 1 starts with the strategy DSL, the canonical JSON IR, and schema validation. No wallets, keys, signing, or swaps.

## Layout

| Path | What it is |
| --- | --- |
| `web/` | Next.js placeholder page. No product features. |
| `service/` | FastAPI app. `GET /health`, the auth seam, the IR validator, the expression parser (`app/expr`), and the strategy-file parser (`app/dsl`). |
| `schemas/` | JSON Schema draft 2020-12 for IR v1, plus three valid examples. |
| `docs/dsl-spec.md` | Strategy DSL, IR contract, semantic error codes, and the canonical hash. |
| `docs/save-format.md` | Versioned save envelope (`ir_version`, `format_version`, `ir_hash`). |
| `docs/design-notes.md` | `user_id` on stored rows, and the single-user auth seam. |
| `.github/workflows/ci.yml` | Runs `make test`. |

`ir_version` is the string `"1.0"`. The validator is `service/app/ir/validate.py`. `ir_hash` is sha256 of the canonical JSON.

## Setup

Python 3.11+ and Node.js 22. Creating the virtualenv needs Python's `venv` module (`python3-venv` on Debian or Ubuntu). From the repo root:

```bash
make install
make test
```

`make test` creates `.venv`, installs Python dependencies from the hashed lock `service/requirements.lock`, installs `web/` dependencies when the lockfile changes, then runs ruff, pytest, and the web typecheck and production build. CI runs that same `make test` target.

Copy `.env.example` only if you want a local env file. It has no values. Do not put secrets in it.

Run the API locally, after `make install`:

```bash
.venv/bin/python -m uvicorn app.main:app --app-dir service --port 8000
```

`GET /health` returns `{"status": "ok"}`. The web placeholder is `npm run dev --prefix web`.

## Tests

`make test` is the full check:

- `ruff check` on `service/`
- `pytest` (health, auth seam, schema, valid examples, invalid fixtures, DSL round-trip, canonical hash)
- `tsc --noEmit` and `next build` in `web/`

Valid IR examples live in `schemas/examples/` (RSI cross, EMA cross, Bollinger revert). Invalid fixtures live in `service/tests/fixtures/invalid/`.

## Scope reminder

Spot only, long only, Solana only. Stored strategies carry `user_id`. The running process returns one fixed user from `get_current_user`. There is no signup and no multi-user feature.
