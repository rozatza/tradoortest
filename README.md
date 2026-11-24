# tradoortest

Internal Astral-style strategy trader for one user. Scope is Solana spot, long-only. Phase 1 starts with the strategy DSL, the canonical JSON IR, and schema validation. No wallets, keys, signing, or swaps.

## Layout

| Path | What it is |
| --- | --- |
| `web/` | Next.js author chat, strategy file editor, backtest results, and the paper dashboard. It streams one session over the agent SSE proxy, shows tool steps and proposal cards, edits a saved strategy with explicit validate and save, renders a job's equity curve, metrics, and trade log, and polls the simulated paper book. |
| `service/` | FastAPI app. `GET /health`, the auth seam, the IR validator, the expression parser (`app/expr`), the strategy-file parser (`app/dsl`), the indicator library (`app/indicators`), historical OHLCV ingest (`app/ohlcv`), the live bar feed and paper scheduler (`app/live`), the simulated paper broker (`app/paper`), the backtest engine (`app/backtest`), the backtest job API (`app/jobs`), saved strategies (`app/strategies`), the agent tool layer (`app/tools`), and the author agent loop (`app/agent`). |
| `docs/agent-loop.md` | The author loop, model tiers, both guardrails, the 24 hour persistence rule, and the SSE event contract. |
| `docs/agent-tools.md` | The ten agent tools, their JSON Schemas, preview-before-apply, and the error envelope. |
| `docs/strategies.md` | Strategy CRUD, immutable versions, canonical `ir_hash`, and diffs. |
| `docs/jobs.md` | Async backtest jobs. Idempotent start, status, persisted results, and the SQLite queue. |
| `docs/backtest.md` | How the engine fills market orders on bar close. Simulation only. |
| `docs/metrics.md` | Return, drawdown, and risk ratios, plus the CSV and JSON trade log. |
| `docs/ohlcv.md` | Provider, cost, timeframes, bar checks, and the backfill command. |
| `docs/live-feed.md` | Near-real-time bars, the paper scheduler, closed bars, and the restart cursor. |
| `docs/paper.md` | Simulated paper fills, the 5-minute order expiry, and the position rows that outlive the 24-hour fill log. |
| `data/` | Gitignored local SQLite. `strategies.sqlite` is saved strategies, `backtest-jobs.sqlite` is the job queue, `ohlcv.sqlite` is the OHLCV cache, and `agent.sqlite` is the author transcript. The cache, job results, and pool rows expire within 24 hours. The agent file does not keep a second copy of those payloads. |
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

`make test` creates `.venv`, installs Python dependencies from the hashed lock `service/requirements.lock`, installs `web/` dependencies when the lockfile changes, then runs ruff, pytest, and the web typecheck, Vitest suite, and production build. CI runs that same `make test` target. The web tests mock the author API. They do not call a model or a market data host.

Copy `.env.example` only if you want a local env file. It has no values. Do not put secrets in it.

Run the API locally, after `make install`:

```bash
.venv/bin/python -m uvicorn app.main:app --app-dir service --port 8000
```

`GET /health` returns `{"status": "ok"}`. `POST /backtests` starts a simulation job for the local user. Status, results, and exports are `GET /backtests/{job_id}`, `GET /backtests/{job_id}/results`, and `GET /backtests/{job_id}/exports/{name}`. The queue is a SQLite file, `data/backtest-jobs.sqlite`, which git ignores. Details are in `docs/jobs.md`. Saved strategies are a different SQLite file, `data/strategies.sqlite`, also gitignored. `POST /strategies` validates DSL or IR and stores version 1. `PUT /strategies/{strategy_id}` appends the next immutable version. Diffs, archive, and the env vars are in `docs/strategies.md`. `GET /tools` lists the agent tools and `POST /tools/{tool_name}` runs one. Contracts, the preview-before-apply rule, and the error codes are in `docs/agent-tools.md`. `POST /agent/sessions` opens an author session and `POST /agent/sessions/{session_id}/messages` runs one turn. Edits stay pending until `POST /agent/proposals/{proposal_id}/accept`. The provider seam, caps, guardrails, and SSE frames are in `docs/agent-loop.md`. The transcript is `data/agent.sqlite`, also gitignored. The chat and strategy editor are `npm run dev --prefix web`. Set `AGENT_API_ORIGIN` in the environment (see the commented line in `.env.example`) to the API origin, for example `http://127.0.0.1:8000`. The Next server proxies `/api/agent`, the strategy file routes under `/api/strategies`, the backtest job, result, and export routes under `/api/backtests`, and `GET /api/paper` to that origin. `/paper` is the simulated paper dashboard. The value is not a `NEXT_PUBLIC_` variable and is not sent to the browser. Saves send `expected_version` and `expected_ir_hash`. A stale save is shown as a conflict and is not retried.

Backfill SOL and four native majors (JUP, JTO, BONK, WIF) after `make install`. This talks to the public GeckoTerminal API and writes `data/ohlcv.sqlite`, which git ignores. That file is a cache and expires within 24 hours. No API key is required. Data by GeckoTerminal / CoinGecko. Any other Solana mint is `--mints`. GeckoTerminal stays the primary source. Birdeye is an optional fallback and stays off unless `BIRDEYE_API_KEY` is set in the environment. Details, rate limits, and the gap report are in `docs/ohlcv.md`. A local live check, not part of CI, is `scripts/acceptance_roz92.py`.

`LIVE_FEED_ENABLED` starts a background scheduler that polls closed bars for those majors and records one runner decision per strategy version. It is off when the variable is unset. It does not send orders. See `docs/live-feed.md`. When the flag is on, the same tick stores the fetched bars and then runs `PaperBroker.tick` for simulated fills. `POST /strategies/{strategy_id}/versions/{version}/paper` deploys one saved version. `DELETE` on that path undeploys it. The running-paper cap defaults to 3 (`PAPER_MAX_RUNNING`). See `docs/paper.md`.

```bash
.venv/bin/python -m app.ohlcv.backfill --days 90
```

## Tests

`make test` is the full check:

- `ruff check` on `service/`
- `pytest` (health, auth seam, schema, valid examples, invalid fixtures, DSL round-trip, canonical hash)
- `tsc --noEmit`, Vitest, and `next build` in `web/`

Valid IR examples live in `schemas/examples/` (RSI cross, EMA cross, Bollinger revert). Invalid fixtures live in `service/tests/fixtures/invalid/`.

## Scope reminder

Spot only, long only, Solana only. Stored strategies carry `user_id`. The running process returns one fixed user from `get_current_user`. There is no signup and no multi-user feature.
