# Historical OHLCV

SOL and four other native Solana assets. Bars are 1 minute through 1 day. Phase 1 keeps them in a local SQLite file behind the store seam. That file is a cache, not a history database. Gaps are spans the provider did not answer.

This is one user. Each row has `user_id`, always the auth seam's `local-user`. The CLI does not take a user id. There is no account list and no sharing. There are no wallets, keys, signatures, or swaps.

## Phase 1 choice

ROZ-69 named Birdeye, Bitquery, and Helius, with Postgres or Parquet for storage. Phase 1 does not do that. It uses the keyless GeckoTerminal API and SQLite behind `BarStore`. A long-history store, with a licensed provider, is deferred to ROZ-92.

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
| Rate | CoinGecko documents about 5 calls per minute for the keyless pool (shared by IP, and it moves). The CLI waits 12 seconds between calls. HTTP 429, HTTP 5xx, and timeouts back off and retry, at most five attempts. A connect or DNS failure is reported once and is not retried. The error text does not include the response body. Response headers are not stored or logged. |
| History | The public API serves about the past 180 days. `--days` above 180 exits before any request, with that limit in the message. A 401 from the API is reported the same way, not as a bare status code. Analyst and above goes back to September 2021, depending on when the pool was tracked. This project does not use a paid plan. |
| Empty minutes | Omitted by the API. The request never sets `include_empty_intervals`. That flag would copy the previous close forward without saying so. |

90 days of 1-minute bars was checked on 2026-11-19. For each pool below, a 1-minute candle existed at or within 10 minutes before 2026-08-21.

A dense 90-day 1-minute fetch is 129,600 minutes. At 1,000 candles per call that is 130 requests per asset. At 5 calls per minute (one call every 12 seconds) that is 1,560 seconds, 26 minutes, per asset. Five assets are about 650 requests and about 130 minutes. A thin pool can be fewer calls: one page of 1,000 candles can cover more than 1,000 minutes when many minutes had no trade. Higher timeframes are their own native requests. They are not free. The CLI prints `ohlcv:` progress lines on stderr while it fetches. The cache does not have to outlive the run. Acceptance is a backtest-ready 90-day 1-minute series for SOL and the four majors inside the TTL.

### Assets

Wrapped BTC and wrapped ETH are not in the list. These five are large assets that trade on Solana: SOL, the Jupiter token, the Jito token, Bonk, and dogwifhat.

The registry (`service/app/ohlcv/registry.py`) is symbol to mint only. Pool addresses live in the GeckoTerminal adapter. A later source that can take any mint implements `fetch_bars(mint, timeframe, start, end)` and does not change the backfill. A durable history store, with a licensed provider, is ROZ-92.

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
| Birdeye | On-chain | Yes (`X-API-KEY`) | Free Standard is 30,000 compute units per month, 1 request/second, and limited data access. One 90-day 1m pass might fit the unit budget. The plan does not promise that history. | Free tier, then paid | Named by ROZ-69. Not built. A licensed long-history store is ROZ-92. |
| Bitquery | On-chain | Yes | Named by ROZ-69. Not evaluated as the Phase 1 source. | Paid plans | Not used. |
| Helius | On-chain RPC | Yes | No OHLCV product. Bars would be derived from transactions. | Free tier with a key | Named by ROZ-69. Not built. A licensed long-history store is ROZ-92. |

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

On 1m, a short run of minutes inside a fetched span with no candle becomes flat synthetic bars: open, high, low, and close are the previous real close, volume is 0, and `synthetic` is true. That flag is stored and it is on the bar the store returns. It is a no-trade minute, and it is not a trade. A run is at most 15 consecutive minutes. A longer run is a gap, not a row of flat bars, so a multi-hour outage is not stored as hours of silent synthetics. The 15 minutes are counted across the whole fetched span, not only inside the current window.

The previous close is the latest real close in that same fetched span. A later window that starts in the middle of a span already fetched still uses that close, so the synthetic bars at the new window's start stay. The close does not come from before the span, and it does not cross an unfetched hole. Two runs that cover the same spans store the same series, whichever window was requested first.

Minutes before the first real bar in a span have no prior close, so they stay missing and the gap report lists them. A rejected minute stays rejected. It is not turned into a synthetic bar and it is not a gap. 5m and above are stored only as the provider returned them. A missing native bucket inside a fetched span is a gap. Nothing is resampled from 1m and nothing is copied forward.

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

`BarStore` in `service/app/ohlcv/store.py` is the seam. `SqliteBarStore` is the Phase 1 cache. A Postgres or Parquet implementation would be another class with the same methods. No Postgres driver is installed, and no database URL is read. That durable store is ROZ-92, with a licensed provider.

The file is `ohlcv.sqlite` under `OHLCV_DATA_DIR`, or `./data` when that variable is unset. `data/` is gitignored. Do not commit bar files, gap reports, or SQLite sidecars. The committed fixtures are synthetic and tiny, and each one is labeled `synthetic: true`.

The TTL defaults to 24 hours. `--cache-ttl-hours` or `OHLCV_CACHE_TTL_HOURS` can set a shorter one. A value above 24 hours is rejected with a message about the CoinGecko API terms. It is not clamped. `nan` and `inf` are rejected with that same one-line message. On open, on every read and write, and on close, rows, rejections, and fetched-range records with `stored_at + ttl <= now` are deleted. They are treated as missing and fetched again. Each fetched page is its own row with its own clock, so a newer page does not keep an older page alive. A synthetic bar uses the `stored_at` of the real close it copies, so it expires with that trade.

The primary key of a bar is `(user_id, mint, timeframe, open_time)`. Inserting the same key again replaces the OHLC values. The same input leaves the same row.

## Gap report

The CLI prints JSON and writes the same document when `--report` is set. `forward_filled` is always false: the provider is not asked to copy a close forward. `synthetic_no_trade` is true because 1m no-trade minutes are stored as flagged flat bars. `attribution` is `Data by GeckoTerminal / CoinGecko`.

Counts are per symbol and timeframe. `coverage_pct` is real bars divided by the expected grid, as a percent. `real` counts provider candles. `synthetic` counts flagged no-trade bars. `present` is those two. `missing` is `expected - present`. `unfetched` counts grid points no fetched span covers. `gaps` includes those points and every other grid point that has no stored bar, except a rejected minute. A leading range is a gap when the provider's history starts inside the window, or when a short page does not reach back to the window start. A 1m no-trade run longer than 15 minutes is a gap. A missing 5m, 15m, 1h, 4h, or 1d bucket inside a fetched span is a gap. A rejected minute is listed under `rejected` and is not a gap. A flagged synthetic bar is not a gap.

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

The 00:01 candle was not repaired. 00:02 and 00:04 are synthetic bars with volume 0 and `synthetic: true`. Each of those no-trade runs is one minute, under the 15-minute cap. A later run inside the TTL does not request the span again.

A window whose first candle is later than the start lists that leading range under `gaps` even though the span was fetched. `unfetched` stays 0 for that range. The same is true of a native 5m bucket the provider omitted.

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

`--symbols` is case-insensitive. An empty list, such as `--symbols ,`, exits 1. `--days` above 180 exits 1 before any request. `--cache-ttl-hours` above 24 hours, or `nan`, or `inf`, does too. Usage errors from the argument parser also exit 1. `python -m app.ohlcv` is the same command. While a fetch is running, stderr gets lines that start with `ohlcv:`. A connect or DNS failure, a timeout that exhausts its retries, an unwritable data directory, `--data-dir /dev/null`, a report path whose parent is not a directory, and a SQLite error each exit 1 with one error line on stderr and no traceback. The report path is checked before any request. Ctrl-C exits 130 and does not print a traceback. Progress lines are separate from that error line.

## Environment

| Variable | Required | Meaning |
| --- | --- | --- |
| `OHLCV_DATA_DIR` | No | Directory for `ohlcv.sqlite`. Default `./data`. |
| `OHLCV_CACHE_TTL_HOURS` | No | How long cached rows are kept. Default 24. Above 24 is rejected. |

There is no provider API key variable. `.env.example` lists the names and has no values.

## Tests

`make test` does not call the network. `service/tests/conftest.py` blocks `socket.connect`, `socket.connect_ex`, `socket.create_connection`, and DNS for every test. The adapter tests use `httpx.MockTransport` and the committed fixtures under `service/tests/fixtures/ohlcv/`. Both fixtures are synthetic and labeled `synthetic: true`. They have no headers, no key, and no account id. `geckoterminal_sparse_synthetic.json` is shaped like a thin JUP or WIF pool. It is not a live response.
