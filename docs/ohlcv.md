# Historical OHLCV

SOL and four other native Solana assets. Bars are 1 minute through 1 day. The store is a local SQLite file. Gaps stay gaps.

This is one user. Each row has `user_id`, always the auth seam's `local-user`. The CLI does not take a user id. There is no account list and no sharing. There are no wallets, keys, signatures, or swaps.

## Provider

**GeckoTerminal keyless public API.** On-chain DEX pool candles. Not a CEX.

| | |
| --- | --- |
| Base URL | `https://api.geckoterminal.com/api/v2` |
| Endpoint | `GET /networks/solana/pools/{pool}/ohlcv/minute` |
| Key | None. The client sends `Accept` and `User-Agent` only. |
| Price | `currency=usd` and `token=<mint>`, so the series is that mint in USD even when the pool's quote asset is SOL. |
| Volume | The USD volume figure GeckoTerminal returns on that candle. It is that pool's volume, not a CEX print, and it is not comparable across pools. |
| Page size | 1000 candles. |
| Rate | CoinGecko documents about 5 calls per minute for the keyless pool (shared by IP, and it moves). The CLI waits 12 seconds between calls. HTTP 429, HTTP 5xx, and timeouts back off and retry, at most five attempts. A connect or DNS failure is reported once and is not retried. The error text does not include the response body. Response headers are not stored or logged. |
| History | The public API serves about the past 180 days. `--days` above 180 exits before any request, with that limit in the message. A 401 from the API is reported the same way, not as a bare status code. Analyst and above goes back to September 2021, depending on when the pool was tracked. This project does not use a paid plan. |
| Empty minutes | Omitted. The request never sets `include_empty_intervals`. That flag would copy the previous close forward and set volume to 0. That is forward-fill, and it is not used. |

90 days of 1-minute bars was checked on 2026-11-19. For each pool below, a 1-minute candle existed at or within 10 minutes before 2026-08-21. A full backfill is about 130 requests per asset (90 × 1440 / 1000), because each asset is one contiguous unfetched span, paged at 1000 candles. Five assets is about 650 requests, about 130 minutes at a 12-second gap. Each page is committed before the next request. Stop and run the command again: spans already fetched are not requested again. A minute inside a fetched span with no candle had no trades. It is not a hole to refetch.

CoinGecko asks for attribution when this data is displayed. This repository stores it and does not render it. A later UI has to add that attribution.

### Assets

Wrapped BTC and wrapped ETH are not in the list. These five are large assets that trade on Solana: SOL, the Jupiter token, the Jito token, Bonk, and dogwifhat.

The registry (`service/app/ohlcv/registry.py`) is symbol to mint only. Pool addresses live in the GeckoTerminal adapter. A later source that can take any mint (ROZ-92, Birdeye or Helius) implements `fetch_bars(mint, timeframe, start, end)` and does not change the backfill.

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
| Birdeye | On-chain | Yes (`X-API-KEY`) | Free Standard is 30,000 compute units per month, 1 request/second, and limited data access. One 90-day 1m pass might fit the unit budget. The plan does not promise that history. | Free tier, then paid | Not built. That is ROZ-92. |
| Helius | On-chain RPC | Yes | No OHLCV product. Bars would be derived from transactions. | Free tier with a key | Not built. That is ROZ-92. |

No paid tier is used.

## Interface

```text
fetch_bars(mint, timeframe, start, end) -> bars
```

`start` and `end` are unix seconds, half-open. `mint` is the Solana mint. The result lists parsed candles inside the window and candles that could not be parsed. Missing minutes are absent. Nothing is forward-filled.

`service/app/ohlcv/source.py` is the protocol. `service/app/ohlcv/adapters/__init__.py` (`open_source`) is where another name is added.

## Timeframes

| Timeframe | Seconds | How it is built |
| --- | --- | --- |
| 1m | 60 | Native GeckoTerminal minute candles (`aggregate=1`). |
| 5m | 300 | Resampled from 1m. |
| 15m | 900 | Resampled from 1m. |
| 1h | 3600 | Resampled from 1m. |
| 4h | 14400 | Resampled from 1m. |
| 1d | 86400 | Resampled from 1m. UTC midnight. |

Resample buckets are aligned to the unix epoch. A bucket is written only when every constituent 1-minute bar is present. One missing minute drops the whole bucket. Input order does not matter. A duplicate minute drops the bucket.

For a complete bucket, in time order:

- open is the first minute's open
- high is the max high
- low is the min low
- close is the last minute's close
- volume is the sum of the minute volumes

The backfill always stores 1m. Higher timeframes are rebuilt from the stored 1m rows for that window, so they do not add HTTP calls. Rebuilding deletes previously stored higher-timeframe rows in the window first, so a bucket that is no longer complete does not keep an old candle.

The window is at least `--days` long (default 90). It is floored to the coarsest requested timeframe, which can add less than one of those bars.

## Bar checks

Every 1m candle is checked. A failure is listed on the report and the candle is not stored. No field is rewritten.

- open, high, low, close, and volume are finite
- each price is `> 0` and `<= 1e15` (`1e15` itself is allowed). A price of 0 is rejected
- volume is finite, `>= 0`, and `<= 1e15`. A zero-volume bar is kept
- `high >= max(open, close) >= min(open, close) >= low`
- `open_time` is an integer on the timeframe grid

`1e15` is the ceiling because an input above about `1e154` overflows the Bollinger square and makes SMA, EMA, and ATR non-finite. The indicator functions do not clamp. See `docs/dsl-spec.md`.

A flat candle (high equals low) is valid market data. RSI and stochastic rules for a flat window are in the indicator spec. They are not applied by ingest.

A rejected minute is also a gap: it was not stored, and the neighbors are not copied into it.

## Storage

`BarStore` in `service/app/ohlcv/store.py` is the seam. `SqliteBarStore` is the implementation that ships. A Postgres implementation would be another class with the same methods. No Postgres driver is installed, and no database URL is read.

The file is `ohlcv.sqlite` under `OHLCV_DATA_DIR`, or `./data` when that variable is unset. `data/` is gitignored. Do not commit bar files, gap reports, or SQLite sidecars.

The primary key is `(user_id, mint, timeframe, open_time)`. Inserting the same key again replaces the OHLC values. The same input leaves the same row.

## Gap report

The CLI prints JSON. `forward_filled` is always false. Counts are per symbol and timeframe. `gaps` are half-open ranges of minutes that were not stored. On a 1m series, `unfetched` and `unfetched_gaps` are the spans a later run will still request. `no_trade` and `no_trade_gaps` are minutes inside a span that was already fetched and came back without a candle. A rejected minute is in `rejected`. It is not a no-trade minute and it is not refetched. `missing` is `expected - present`, so it counts unfetched minutes, no-trade minutes, and rejected minutes together. Resampled timeframes leave the unfetched / no-trade split at zero; their `gaps` are buckets the 1m set did not complete.

Synthetic example (not a live response). SOL, five 1-minute slots, two good candles, one candle whose high does not cover the close, and two minutes the source never returned:

```json
{
  "user_id": "local-user",
  "provider": "memory",
  "generated_at": "2026-01-01T00:05:00Z",
  "forward_filled": false,
  "series": [
    {
      "symbol": "SOL",
      "mint": "So11111111111111111111111111111111111111112",
      "timeframe": "1m",
      "start": "2026-01-01T00:00:00Z",
      "end": "2026-01-01T00:05:00Z",
      "expected": 5,
      "present": 2,
      "missing": 3,
      "unfetched": 0,
      "no_trade": 2,
      "gaps": [
        {
          "start": "2026-01-01T00:01:00Z",
          "end": "2026-01-01T00:03:00Z",
          "missing": 2
        },
        {
          "start": "2026-01-01T00:04:00Z",
          "end": "2026-01-01T00:05:00Z",
          "missing": 1
        }
      ],
      "unfetched_gaps": [],
      "no_trade_gaps": [
        {
          "start": "2026-01-01T00:02:00Z",
          "end": "2026-01-01T00:03:00Z",
          "missing": 1
        },
        {
          "start": "2026-01-01T00:04:00Z",
          "end": "2026-01-01T00:05:00Z",
          "missing": 1
        }
      ],
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

The 00:01 candle is both rejected and inside the first gap. It was not repaired. 00:02 and 00:04 are no-trade minutes: the window was fetched, and those minutes had no candle. A later run does not request them.

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
  --report data/gap-report.json
```

`--symbols` is case-insensitive. An empty list, such as `--symbols ,`, exits non-zero. `python -m app.ohlcv` is the same command. A connect failure, a timeout that exhausts its retries, an unwritable data directory, `--data-dir /dev/null`, a report path whose parent is not a directory, and a SQLite error each exit non-zero with one line on stderr and no traceback.

## Environment

| Variable | Required | Meaning |
| --- | --- | --- |
| `OHLCV_DATA_DIR` | No | Directory for `ohlcv.sqlite`. Default `./data`. |

There is no provider API key variable. `.env.example` lists the name and has no value.

## Tests

`make test` does not call the network. `service/tests/conftest.py` blocks `socket.connect`, `socket.connect_ex`, `socket.create_connection`, and DNS for every test. The adapter tests use `httpx.MockTransport` and the committed fixture `service/tests/fixtures/ohlcv/geckoterminal_minute.json`, which is synthetic: no headers, no key, no account id.
