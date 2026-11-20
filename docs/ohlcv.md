# Historical OHLCV

SOL and four other native Solana assets. Bars are 1 minute through 1 day. Phase 1 keeps them in a local SQLite file behind the store seam. That file is a cache, not a history database. Gaps are spans the provider did not answer.

This is one user. Each row has `user_id`, always the auth seam's `local-user`. The CLI does not take a user id. There is no account list and no sharing. There are no wallets, keys, signatures, or swaps.

## Phase 1 choice

ROZ-69 named Birdeye, Bitquery, and Helius, with Postgres or Parquet for storage. Phase 1 does not do that. It uses the keyless GeckoTerminal API and SQLite behind `BarStore`. ROZ-92 adds any Solana mint on that same cache. It does not add a long-history store. Every provider row, including a Birdeye fallback, still expires within 24 hours.

CoinGecko API Terms section 6 does not allow a persistent store of API data. The terms are at <https://www.coingecko.com/en/api_terms>. Cached rows and fetched-range records live at most 24 hours. They are deleted on open, on every read and write, and when the store is closed, then fetched again. Attribution: data by GeckoTerminal / CoinGecko. The gap report includes that line.

## Provider

**GeckoTerminal keyless public API.** On-chain DEX pool candles. Not a CEX.

| | |
| --- | --- |
| Base URL | `https://api.geckoterminal.com/api/v2` |
| Endpoints | `GET /networks/solana/pools/{pool}/ohlcv/minute`, `/ohlcv/hour`, and `/ohlcv/day` |
| Key | None. The client sends `Accept` and `User-Agent` only. |
| Price | `currency=usd` and `token=<mint>`, so the series is that mint in USD even when the pool's quote asset is SOL. |
| Volume | The USD volume figure GeckoTerminal returns on that candle. It is that pool's volume, not a CEX print, and it is not comparable across pools. |
| Page size | 1000 candles. |
| Rate | CoinGecko documents about 5 calls per minute for the keyless pool (shared by IP, and it moves). The CLI waits 12 seconds between calls. HTTP 429, HTTP 5xx, timeouts, and transport errors (connect, read, write, and remote protocol) back off and retry, at most five attempts. Other 4xx responses are not retried. A bad proxy, an unsupported URL scheme, and a local protocol error are not retried. The error text does not include the response body. Response headers are not stored or logged. |
| History | The public API serves about the past 180 days. `--days` above 180 exits before any request, with that limit in the message. A 401 from the API is reported the same way, not as a bare status code. Analyst and above goes back to September 2021, depending on when the pool was tracked. This project does not use a paid plan. |
| Empty minutes | Omitted by the API. The request never sets `include_empty_intervals`. That flag would copy the previous close forward without saying so. |

90 days of 1-minute bars was checked on 2026-11-19. For each pool below, a 1-minute candle existed at or within 10 minutes before 2026-08-21.

A dense 90-day 1-minute fetch is 129,600 minutes. At 1,000 candles per call that is 130 requests per asset. At 5 calls per minute (one call every 12 seconds) that is 1,560 seconds, 26 minutes, per asset. Five assets are about 650 requests and about 130 minutes. A thin pool can be fewer calls: one page of 1,000 candles can cover more than 1,000 minutes when many minutes had no trade. Higher timeframes are their own native requests. They are not free. The CLI prints `ohlcv:` progress lines on stderr while it fetches. The cache does not have to outlive the run. Acceptance is a backtest-ready 90-day 1-minute series for SOL and the four majors inside the TTL.

### Assets

Wrapped BTC and wrapped ETH are not in the list. These five are large assets that trade on Solana: SOL, the Jupiter token, the Jito token, Bonk, and dogwifhat.

The registry (`service/app/ohlcv/registry.py`) is symbol to mint only. The five majors keep the pool addresses in the GeckoTerminal adapter, and an unknown mint is still refused before a request. Any other mint uses `open_source("any-mint")` or `--mints`, which resolves a pool and then calls the same `fetch_bars(mint, timeframe, start, end)` seam. There is still no durable history store.

Pools were picked on 2026-11-19: created more than 90 days earlier, and the deepest SOL or USDC pair for that mint among pools that old. The deep JUP/USDC pool created 2026-11-10 was skipped because it is newer than the window.

| Symbol | Mint | Pool | Venue | Pair | Pool created |
| --- | --- | --- | --- | --- | --- |
| SOL | `So11111111111111111111111111111111111111112` | `Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE` | Orca | SOL/USDC | 2023-07-05 |
| JUP | `JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN` | `C8Gr6AUuq9hEdSYJzoEpNcdjpojPZwqG5MtQbeouNNwg` | Meteora | JUP/SOL | 2024-03-19 |
| JTO | `jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL` | `2UhFnySoJi6c89aydGAGS7ZRemo2dbkFRhvSJqDX4gHJ` | Orca | JTO/SOL | 2023-12-07 |
| BONK | `DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263` | `5zpyutJu9ee6jFymDGoK7F6S5Kczqtc9FomP3ueKuyA9` | Orca | BONK/SOL | 2023-07-05 |
| WIF | `EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm` | `EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx` | Raydium | WIF/SOL | 2023-11-20 |

### Alternatives

| Source | Where the price comes from | Key | 90 days of 1m | Cost | Why it is not the adapter |
| --- | --- | --- | --- | --- | --- |
| GeckoTerminal keyless | On-chain DEX | No | Yes, checked 2026-11-19 | Free. About 5 calls/minute. About 6 months of history. | This is the adapter. |
| Binance public klines | Off-chain CEX | No | Yes, several years | Free. Higher rate limit. | The book is not a Solana pool. |
| Coinbase Exchange public candles | Off-chain CEX | No | Yes for listed products. 300 candles per call. | Free | Not all five assets are listed. |
| Kraken public OHLC | Off-chain CEX | No | No. About 720 recent candles, roughly 12 hours of 1m. | Free | Not enough history. |
| Birdeye | On-chain | Yes, header `X-API-KEY` from `BIRDEYE_API_KEY` only | Free Standard: 30,000 compute units per month and 1 request/second. `padding` is false. | Free Standard only. No paid-plan headers or a faster default. | Optional fallback inside `any-mint`. Off, and not an error, when the key is unset. |
| Bitquery | On-chain | Yes | Named by ROZ-69. Not evaluated as the Phase 1 source. | Paid plans | Not used. |
| Helius | On-chain RPC | Yes | No OHLCV product. Bars would be derived from transactions. | Free tier with a key | Named by ROZ-69. Not built. |

No paid tier is used.

## Interface

```text
fetch_bars(mint, timeframe, start, end) -> bars
```

`start` and `end` are unix seconds, half-open. `mint` is the Solana mint. The result lists parsed candles inside the window and candles that could not be parsed. The provider does not forward-fill. On 1m, the cache adds flagged synthetic bars for no-trade minutes, described below.

`service/app/ohlcv/source.py` is the protocol. `service/app/ohlcv/adapters/__init__.py` (`open_source`) is where another name is added.

## Timeframes

| Timeframe | Seconds | GeckoTerminal candle |
| --- | --- | --- |
| 1m | 60 | `/ohlcv/minute`, `aggregate=1` |
| 5m | 300 | `/ohlcv/minute`, `aggregate=5` |
| 15m | 900 | `/ohlcv/minute`, `aggregate=15` |
| 1h | 3600 | `/ohlcv/hour`, `aggregate=1` |
| 4h | 14400 | `/ohlcv/hour`, `aggregate=4` |
| 1d | 86400 | `/ohlcv/day`, `aggregate=1` |

Each requested timeframe is fetched on its own. Higher timeframes are not built from 1-minute bars. There is no 1m resample path.

On 1m, minutes inside a fetched span with no candle become flat synthetic bars: open, high, low, and close are the previous real close, volume is 0, and `synthetic` is true. That flag is stored and it is on the bar the store returns. It is a no-trade minute, and it is not a trade. The run can be any length. A long quiet stretch stays synthetic, so it counts as present and it is not a gap. A run strictly longer than the threshold (default 120 minutes, `--long-no-trade-minutes`) is listed under `long_no_trade` and sets `suspect_outage`. The bars stay in the series. The length is counted inside the reported window.

The previous close is the latest real close in that same fetched span. When a page reached back before the window and returned an older candle, that candle is the prior close, and the quiet minutes in the window stay synthetic. They are not a gap. If every older candle on that page is unusable, older pages are read until a usable close is found, the page comes back short, or the oldest candle is more than 24 hours before the window. A seed older than 24 hours is not used. A token that last traded 10 days ago does not fill the requested day with synthetic minutes back to that trade. A later window that starts in the middle of a span already fetched still uses that close, so the synthetic bars at the new window's start stay. The close does not cross an unfetched hole or an empty provider page. Two runs that cover the same spans store the same series, whichever window was requested first. A later process reads the pool stored with those bars and the provider recorded on each fetched span. A window whose spans came from both GeckoTerminal and Birdeye stays `mixed`, and the attribution names both. The bars do not change with run order, and a new process does not relabel a Birdeye span as GeckoTerminal.

The prior close is chosen from every older candle, including candles on the next older page when the last page's older candles are all bad. A candle that did not parse, a candle that fails the bar checks, and a second candle for the same open time are skipped. The conflicting candle is not stored as a real bar, a rejection row for that minute is not deleted, and synthetic bars do not copy it. The next older candle that did parse and pass the checks is the close. A skipped candle that sits between that close and the window stays a rejection, including when the later window is fetched before the window that contains the bad candle. The rejection text names the candle by its open time (`candle at <unix second>`), not by its position on the page, so two request orders store the same text. An unparseable newest candle therefore does not become a synthetic bar, in either run order.

A source that never sets `provider_empty` cannot report an empty-page gap. The flag defaults to false. `iter_pages` leaves it false for a source that only implements `fetch_bars`. An empty response is then an ordinary fetched span. If an earlier real close is already in an abutting span, synthetic bars copy that close across the response, and the report does not call the range a gap. GeckoTerminal sets the flag when the response has no candles at all. A later source, including the ROZ-92 adapter, has to set it the same way or it cannot tell that outage from a quiet market.

Minutes before the first real bar in a span have no prior close, so they stay missing and the gap report lists them. That is the leading range when history starts inside the window, or when a short page does not reach back past the window start. A page is empty only when the provider returned no candles at all. That empty page, and a fetch that never covered the range, are gaps. A page that returned older candles is not empty. A rejected minute stays rejected. It is not turned into a synthetic bar and it is not a gap. 5m and above are stored only as the provider returned them. A missing native bucket after the first real candle in the merged span is a no-trade bucket: `no_trade` counts it and `coverage_pct` is still real bars over the grid. It is not one gap range per bucket, and a later page does not open a new leading gap at the seam. Nothing is resampled from 1m and nothing is copied forward.

The window is at least `--days` long (default 90). It is floored to the coarsest requested timeframe, which can add less than one of those bars.

## Bar checks

Every candle is checked. A failure is listed on the report and the candle is not stored. No field is rewritten.

- open, high, low, close, and volume are finite
- each price is `> 0` and `<= 1e15` (`1e15` itself is allowed). A price of 0 is rejected
- volume is finite, `>= 0`, and `<= 1e15`. A zero-volume bar is kept
- `high >= max(open, close) >= min(open, close) >= low`
- `open_time` is an integer on the timeframe grid

`1e15` is the ceiling because an input above about `1e154` overflows the Bollinger square and makes SMA, EMA, and ATR non-finite. The indicator functions do not clamp. See `docs/dsl-spec.md`.

A flat candle (high equals low) is valid market data. RSI and stochastic rules for a flat window are in the indicator spec. They are not applied by ingest.

A synthetic bar has to be flat, with volume 0. A fetched page that already carries the synthetic flag is refused.

A rejected minute is missing. It is not a gap when the provider already answered that span, and the neighbors are not copied into it.

## Storage

`BarStore` in `service/app/ohlcv/store.py` is the seam. `SqliteBarStore` is the cache. A Postgres or Parquet implementation would be another class with the same methods. No Postgres driver is installed, and no database URL is read. ROZ-92 did not add that store. Pool choices and ingest job results live in the same SQLite file and expire on the same clock as the bars.

The file is `ohlcv.sqlite` under `OHLCV_DATA_DIR`, or `./data` when that variable is unset. `data/` is gitignored. Do not commit bar files, gap reports, or SQLite sidecars. The committed fixtures are synthetic and tiny, and each one is labeled `synthetic: true`.

The TTL defaults to 24 hours. `--cache-ttl-hours` or `OHLCV_CACHE_TTL_HOURS` can set a shorter one. A value above 24 hours is rejected with a message about the CoinGecko API terms. It is not clamped. `nan` and `inf` are rejected with that same one-line message. On open, on every read and write, and on close, rows, rejections, fetched-range records, pool choices, and ingest results with `stored_at + ttl <= now` are deleted. A Birdeye series uses that same purge. It is not kept longer. They are treated as missing and fetched again. Each fetched page is its own row with its own clock, so a newer page does not keep an older page alive. A synthetic bar uses the `stored_at` of the real close it copies, so it expires with that trade.

The primary key of a bar is `(user_id, mint, timeframe, open_time)`. Inserting the same key again replaces the OHLC values. The same input leaves the same row.

## Gap report

The CLI prints JSON and writes the same document when `--report` is set. `forward_filled` is always false: the provider is not asked to copy a close forward. `synthetic_no_trade` is true because 1m no-trade minutes are stored as flagged flat bars. `attribution` is `Data by GeckoTerminal / CoinGecko`, or `Data by Birdeye` when every series in that run was served by the fallback.

Counts are per symbol and timeframe. `coverage_pct` is real bars divided by the expected grid, as a percent. `real` counts provider candles. `synthetic` counts flagged no-trade bars. `present` is those two, so a quiet 1m minute counts as present and is not missing. `missing` is `expected - present`. `unfetched` counts grid points no fetched span covers. `no_trade` is the synthetic count on 1m, and on 5m and above it is the number of omitted native buckets after history has started. `long_no_trade_minutes` is the threshold for this report. `long_no_trade` lists quiet runs strictly longer than that threshold, with `start`, `end`, and `minutes`. `suspect_outage` is true when that list is not empty. Those runs are not gaps.

`gaps` are provider-missing ranges only. A leading range is a gap when the provider's history starts inside the window, or when a short page does not reach back past the window start. An empty page (no candles in the response) and a span the fetch never covered are gaps. A rejected minute is listed under `rejected` and is not a gap. A flagged synthetic bar is not a gap. A missing 5m, 15m, 1h, 4h, or 1d bucket after the first real candle in the merged span is `no_trade`, not a gap range.

Synthetic example (not a live response). SOL, five 1-minute slots. The span was fetched. 00:00 and 00:03 are real. 00:01 is rejected because high does not cover the close, so it is not a synthetic bar. 00:02 copies the 00:00 close. 00:04 copies the 00:03 close. Coverage is 2 / 5.

```json
{
  "user_id": "local-user",
  "provider": "memory",
  "generated_at": "2026-01-01T00:05:00Z",
  "attribution": "Data by GeckoTerminal / CoinGecko",
  "forward_filled": false,
  "synthetic_no_trade": true,
  "series": [
    {
      "symbol": "SOL",
      "mint": "So11111111111111111111111111111111111111112",
      "timeframe": "1m",
      "start": "2026-01-01T00:00:00Z",
      "end": "2026-01-01T00:05:00Z",
      "expected": 5,
      "real": 2,
      "synthetic": 2,
      "present": 4,
      "missing": 1,
      "coverage_pct": 40.0,
      "unfetched": 0,
      "no_trade": 2,
      "long_no_trade_minutes": 120,
      "long_no_trade": [],
      "suspect_outage": false,
      "gaps": [],
      "rejected": [
        {
          "open_time": "2026-01-01T00:01:00Z",
          "reasons": ["high is below max(open, close)"]
        }
      ]
    }
  ]
}
```

The 00:01 candle was not repaired. 00:02 and 00:04 are synthetic bars with volume 0 and `synthetic: true`. Each of those no-trade runs is one minute, so neither is a long-run flag. A later run inside the TTL does not request the span again.

A window whose first candle is later than the start lists that leading range under `gaps` even though the span was fetched. `unfetched` stays 0 for that range. A native 5m bucket the provider omitted after the first candle is `no_trade`, not a gap. An empty page is a response with no candles at all: history that has not started, or a short page that comes back empty. It is a gap, and it is not filled with synthetic bars. A real mid-history GeckoTerminal outage does not look like that. The API returns the older candles, the quiet minutes stay synthetic, and a run longer than the threshold sets `suspect_outage`.

## Any Solana mint

`--mints` takes comma-separated mint addresses. It does not take the five symbols. Pass `--symbols` or `--mints`, not both. An empty `--mints` exits 1. A mint that is not the base58 encoding of 32 bytes is `invalid mint` before any request, and the text does not echo the input. The five majors are unchanged: `GeckoTerminalSource` still refuses a mint that is not in its pool map before a request.

The any-mint source asks GeckoTerminal for pools, one page of up to 20 by default (`--pool-pages` raises that). GeckoTerminal orders that page by a blend of liquidity and volume. This code re-ranks it. The tie-break, among eligible DEX pools, is:

1. Highest `reserve_in_usd`, as a base-10 decimal. A missing, non-numeric, or negative reserve is not eligible.
2. If liquidity is equal, the earlier `pool_created_at` wins. A missing timestamp loses.
3. If those still tie, the lexicographically smallest pool address wins.

A pump.fun bonding curve is not eligible. That is a dex id of `pump-fun`, `pumpfun`, or `pump.fun`, or `launchpad_details.completed` set to false. PumpSwap (`pumpswap`) is a DEX pool after migration and stays eligible. When every candidate is a bonding curve, the error is `no DEX pool yet` (API `no_dex_pool_yet`, HTTP 422). When there is no pool and the token is not an ungraduated launchpad, the error is `no pool for mint <mint>` (API `no_pool`, HTTP 404). The CLI exits 1 with that one line.

The chosen pool is on the ingest result, the CLI JSON, and `GET /ohlcv/jobs/{job_id}`: address, dex, dex id, quote token, quote symbol, liquidity at resolution, and `resolved_at`. It is stored with the bars and read back on a cache hit, including from a new process, until the 24-hour purge. Saving the same resolution again does not extend the pool past 24 hours from `resolved_at`. Each series records `provider`: `geckoterminal`, `birdeye`, or `mixed` when the stored spans came from both. The label is the one on the spans, so a cache hit in a fresh process keeps it.

`--days` above 180 is a clear error before any request. The window is not clamped. The API code is `history_limit`, HTTP 400, on `$.days`. That message says days must be 180 or less. It does not mention a CLI flag, and a timeframe that merely contains the digits 180 is not this error.

GeckoTerminal calls share the 12-second pace with the pool lookup. HTTP 429 and HTTP 5xx retry up to five times. The call cap defaults to 180 (`--gt-max-calls` or `OHLCV_GT_MAX_CALLS`). Exhausting the cap or the retries is HTTP 429 `rate_limit`, and the message does not include the response body.

Birdeye `GET /defi/v3/ohlcv` runs only when `BIRDEYE_API_KEY` is set, and only when GeckoTerminal has no pool at all, or returned a DEX pool whose candle page was empty. A pre-migration pump.fun mint is `no DEX pool yet` even when that key is set. Birdeye is not called for a bonding-curve token, and this release does not serve those tokens through Birdeye. A partial series is not replaced. The key is sent as the `X-API-KEY` header. It is not put in the query, a fixture, a log line, an error, or a repr. Unset, blank, or whitespace means the fallback is skipped and is not an error. The pace is 1 request per second, including when several requests run at once. The compute-unit budget defaults to 30,000 (`--birdeye-cu-budget` or `BIRDEYE_CU_BUDGET`) and is not raised on its own. The API reads that same variable. The next call is refused before it is sent when the budget, counting calls already in flight, cannot pay for the candles that were asked for. A successful HTTP 200 is what gets billed. `padding` and `outlier` are false, so a missing minute is not copied forward and a candle is not dropped for us. A month counter is written to `birdeye-cu.json` under the data directory by both the CLI and the API. That file holds the period and the used count. It does not hold the key. The counter is tracked per process: each process keeps the used count in memory and overwrites the file, so an API process and a CLI process running at the same time can undercount against that shared file. The month roll resets the used count. The GeckoTerminal call cap and the Birdeye request cap reset at the start of each ingest (one CLI run, or one POST). They do not stay exhausted for the life of the process.

Flags describe the stored series. They do not add bars. `short_history` means the first real bar is after the window start, or there are fewer real bars than `--warmup-bars`. Nothing is back-filled. `sparse` means fewer than half the expected bars are real, once the grid is at least four bars. `zero_volume` counts real bars whose volume is 0 and keeps them. `young_pool` means the pool was created inside the window, or it is younger than the window. A hole the provider left empty is a gap. Neighbor closes are not averaged and not copied across that hole. A 1m minute inside a fetched span that had no trade is still the existing synthetic bar. That is not a gap and `forward_filled` stays false.

`POST /ohlcv/jobs` runs the ingest for the local user and returns the same JSON. The ingest runs off the event loop, so a slow provider call does not stall `GET /health`. A `user_id` in the body is ignored. One request accepts at most `OHLCV_MAX_MINTS` mints (default 8), counted on the raw list before duplicates are removed. A longer list is HTTP 422 `too_many_mints`. A body larger than `OHLCV_MAX_BODY_BYTES` (default 16384) is HTTP 413 `payload_too_large`. Both messages are fixed and do not echo the body. `GET /ohlcv/jobs/{job_id}` returns the document until the 24-hour purge. The read that deletes an expired row is HTTP 410 `expired`. An id that is already gone is HTTP 404. A locked, corrupt, or unwritable store is HTTP 503 `store_unavailable` with a fixed message and no path, traceback, or SQLite text. An unreadable `birdeye-cu.json` is the same HTTP 503, including when `BIRDEYE_API_KEY` is unset. In-memory notes of a chosen pool or a missing pool expire within 24 hours, so a mint that migrates off a bonding curve is resolved again without restarting the process.

## Run the backfill

From the repo root, after `make install`:

```bash
.venv/bin/python -m app.ohlcv.backfill --days 90
```

Useful flags:

```bash
.venv/bin/python -m app.ohlcv.backfill \
  --days 90 \
  --symbols SOL,JUP,JTO,BONK,WIF \
  --timeframes 1m,5m,15m,1h,4h,1d \
  --data-dir data \
  --report data/gap-report.json \
  --cache-ttl-hours 24
```

`--symbols` is case-insensitive. An empty list, such as `--symbols ,`, exits 1. `--days` above 180 exits 1 before any request, for `--symbols` and for `--mints`. The window is not clamped. `--cache-ttl-hours` above 24 hours, or `nan`, or `inf`, does too. `--long-no-trade-minutes` below 1 does too. Usage errors from the argument parser also exit 1. `python -m app.ohlcv` is the same command. While a fetch is running, stderr gets lines that start with `ohlcv:`. A transport error or a timeout that exhausts its retries, an unwritable data directory, `--data-dir /dev/null`, a report path whose parent is not a directory, and a SQLite error each exit 1 with one error line on stderr and no traceback. The report path is checked before any request, including a parent directory that exists but is not writable. Ctrl-C exits 130 and does not print a traceback. Progress lines are separate from that error line.

Any mint, one day of 5-minute bars:

```bash
.venv/bin/python -m app.ohlcv.backfill \
  --mints <mint> \
  --days 1 \
  --timeframes 5m \
  --data-dir data
```

`--warmup-bars` applies only with `--mints`. It flags a short series. It does not invent bars.

## Environment

| Variable | Required | Meaning |
| --- | --- | --- |
| `OHLCV_DATA_DIR` | No | Directory for `ohlcv.sqlite`. Default `./data`. |
| `OHLCV_CACHE_TTL_HOURS` | No | How long cached rows are kept. Default 24. Above 24 is rejected. |
| `BIRDEYE_API_KEY` | No | Turns on the Birdeye fallback. Unset means off. Never printed. |
| `BIRDEYE_CU_BUDGET` | No | Monthly compute-unit budget for the fallback. Default 30000. Each process keeps its counter in memory and overwrites `birdeye-cu.json`, so concurrent API and CLI processes can undercount the shared file. |
| `OHLCV_GT_MAX_CALLS` | No | GeckoTerminal HTTP calls for one ingest. Default 180. Read by the API and the CLI. The counter resets each ingest. |
| `OHLCV_MAX_MINTS` | No | Mints in one POST, before de-duplication. Default 8. |
| `OHLCV_MAX_BODY_BYTES` | No | Maximum POST body size in bytes. Default 16384. |

`.env.example` lists the names and has no values. The Birdeye key is not one of those values.

## Tests

`make test` does not call the network. `service/tests/conftest.py` blocks `socket.connect`, `socket.connect_ex`, `socket.create_connection`, and DNS for every test. The adapter tests use `httpx.MockTransport` and the committed fixtures under `service/tests/fixtures/ohlcv/`. Those fixtures are synthetic and labeled `synthetic: true`. They have no headers, no key, and no account id. `geckoterminal_sparse_synthetic.json` is shaped like a thin JUP or WIF pool. It is not a live response. `geckoterminal_pools_synthetic.json`, `geckoterminal_pump_synthetic.json`, and `birdeye_ohlcv_synthetic.json` are the any-mint fixtures. `scripts/acceptance_roz92.py` is a local live check. CI does not run it.
