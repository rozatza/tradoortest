# Live bar feed

Near-real-time bars for SOL and the four other native majors (JUP, JTO, BONK, WIF), and a scheduler that calls the shared runner once per closed bar. This is paper state only. The scheduler records the decision. It does not persist a price, fill, attach an OCO, sign, or send an order. There is no deploy route; a strategy version is registered with `register_paper_strategy`.

The runner is `app.runner.step`. The book view passed in is empty, so a decision cannot arm an OCO. A later paper broker would apply the recorded decision. This feed does not.

## Feed

GeckoTerminal is the primary source. The client is the existing keyless adapter: no API key, `Accept` and `User-Agent` only. Birdeye is the fallback, and only after GeckoTerminal raises. It runs only when `BIRDEYE_API_KEY` is set to a non-blank key. Unset, blank, or whitespace means the fallback is disabled: it is not called, and the missing key is not an error. The key stays in the request header. It is not written to the cache, a log line, or an error.

The request window is the closed bars the lookback does not already hold. The lookback is `max(warmup_bars, indicator samples) + 1` bars ending at the newest closed bar, so a 1h SMA(50) asks for 51 bars and a 4h SMA(20) asks for 21 bars. On a tick where that span is already in the cache, the fetch starts at the cursor and only asks for what is new. When a slot in the span is missing, the fetch asks for the missing range through the newest closed bar, once per mint and timeframe on that tick. A closed bar the provider includes from before that start is still stored. Once a cursor exists, a bar older than it is logged late and is not decided again.

Candles are parsed by the existing adapters. Nothing in this path copies a close forward or builds a gap bar. `include_empty_intervals` is not set. Two equal candles for one open time are stored once. Two different candles for one open time are both dropped.

## Closed bars

A bar is closed only when `open_time + bar_seconds + CLOSE_GRACE_SECONDS <= now`. `CLOSE_GRACE_SECONDS` is 60 (`app/live/feed.py`). The clock is injectable. The extra minute means a local clock that is fast by about 40 seconds still does not treat the forming bar as closed. The forming bar is not stored and is not passed to the runner.

A synthetic bar, including a no-trade bar the historical cache may already hold, is not passed to the runner. In the window it is a hole: the series sample is empty and the bar object is absent, so it cannot produce a close or an OCO touch. A missing slot is also not a bar. The scheduler does not invent one.

## Scheduler

The worker is a daemon thread started from the app lifespan and stopped on shutdown. The tick does not run on the request event loop, so `GET /health` stays fast while a tick is in progress. `stop()` sets a flag the tick checks between strategies. Provider backoff waits on that same flag instead of sleeping out the full delay, so shutdown returns while a retry is waiting. The join itself is capped at 5 seconds.

`LIVE_FEED_ENABLED` turns it on (`1`, `true`, `yes`, `on`). Unset or any other value leaves it off. Tests leave it off. With no registrations the thread still starts when the flag is on, and it does not fetch until a version is registered.

Each tick, for each registered `(strategy id, version)`:

1. Fetch the majors in that strategy, one call per mint and timeframe, for the missing lookback or the new bars.
2. If that fetch raises, including a 429 after the bounded retries, record `not_ready` and an error row (exception type and a short message, no response body). Do not decide a bar and do not move the cursor, so the next tick can try again. No bar is invented to fill the gap.
3. Walk closed open times after the cursor, in order.
4. For a real closed bar this version has not already decided, rebuild runner memory from the initial state by calling `step` on every slot in the lookback window, including holes. Then persist the decision. The cursor write stores an empty `state_json`. It does not store a close or an indicator value.
5. A hole is stepped as a missing bar. Memory from before the hole is not carried into the next real bar, so a `cross_*` cannot fire from a pre-gap close.

A missing open time is left alone until the following bar is already closed, using the same grace. A bar that arrives in that gap is still decided once. Once the following bar has closed and the slot is still empty, the slot is logged as missed and the cursor moves past it. A real bar that shows up after that is logged as late and is not decided. A real bar that is decided after the following bar has already closed is also logged as late, and it is still decided once. A second copy of a decided bar is logged as a duplicate and is not decided again. The runner is still called when a new bar is decided, because that decision rebuilds the lookback; the decision itself is written once.

A bar that any registered version has already decided is not overwritten. If a later fetch, including a Birdeye fallback, returns a different candle for that open time, the stored candle stays and the feed logs a warning and a conflict row. The conflict row expires with the cache.

A per-strategy failure is written to `live_errors` as the exception type and a short message. The message does not include the response body. That row expires with the cache. The cursor is not moved past a bar whose `step` raised, so the next tick can try it again.

Two versions do not share a cursor. The same open time is decided once per version.

## Rate limits

The adapters keep their pace: about 12 seconds between GeckoTerminal calls, and 1 second between Birdeye calls. HTTP 429, HTTP 5xx, timeouts, and retryable transport errors sleep a bounded backoff and try at most five times. The live opener passes an RNG. Each sleep is `backoff + rng() * backoff`, where `rng()` is a fraction from 0 to 1. A numeric `Retry-After` on HTTP 429 is used when it is longer. The sleep is capped at 120 seconds. Callers that omit the RNG, including the historical backfill, keep the unjittered backoff. Tests pass the RNG and the sleeper in, so the delays are fixed and no test waits on a wall clock.

## Purge

Fetched bars and raw response bodies use the bar cache. The TTL is 86400 seconds, the same 24-hour cap as the rest of the cache, and the same delete on open, read, write, and close. Recorded decisions, the missed/late log, conflict rows, and error rows expire on that clock too, because a decision carries the bar close. The cursor does not. `live_cursors.state_json` is always empty, including while the strategy is ticking and after a purge, so a skip cannot keep a close alive by refreshing `state_stored_at`. `last_open_time` stays, so those open times are not decided again. The registration stays. It is the strategy IR, not a close.

Bars older than 24 hours leave the cache. A lookback that still needs them is fetched again on the next tick, which stores them with a new timestamp under the same 24-hour cap. That re-fetch is the missing span only, at most once per mint and timeframe per tick, and it uses the same bounded backoff and the same stop flag as any other live request. If the provider errors or the retries run out, the strategy is not-ready for that tick: the failure is logged and stored, and no decision is acted on. An empty successful page is not a failure. The holes stay holes, and the rebuild uses only the real bars that are actually stored.

## Restart

The cursor is `(user, strategy id, version) -> (grid origin, last open time)` in `ohlcv.sqlite`. A new scheduler on that file loads the registrations and continues after `last open time`. A bar at or before that open time is not decided again. A bar that was fetched but not yet committed is still in the cache and is decided on the next tick. Every tick, including the first tick after a restart, rebuilds runner memory from the initial state over the lookback window. The window index starts at the first slot of that window, so warmup runs again inside the window before a later slot can emit a rule. Slots that fall outside the window are not stepped with old memory. When the cache no longer covers the lookback, the tick re-fetches that span before the rebuild.
