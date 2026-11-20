# Live bar feed

Near-real-time bars for SOL and the four other native majors (JUP, JTO, BONK, WIF), and a scheduler that calls the shared runner once per closed bar. This is paper state only. The scheduler records the decision and the runner memory. It does not fill, attach an OCO, sign, or send an order. There is no deploy route; a strategy version is registered with `register_paper_strategy`.

The runner is `app.runner.step`. The book view passed in is empty, so a decision cannot arm an OCO. A later paper broker would apply the recorded decision. This feed does not.

## Feed

GeckoTerminal is the primary source. The client is the existing keyless adapter: no API key, `Accept` and `User-Agent` only. Birdeye is the fallback, and only after GeckoTerminal raises. It runs only when `BIRDEYE_API_KEY` is set to a non-blank key. Unset, blank, or whitespace means the fallback is disabled: it is not called, and the missing key is not an error. The key stays in the request header. It is not written to the cache, a log line, or an error.

The request window is the closed bars the strategy does not have yet. On the first tick the fetch asks for `warmup_bars + 1` closed bars. The cursor then starts at the earliest closed real bar in the cache, so a page that also contains older closed bars is decided in order. After that the fetch starts at the cursor. A closed bar the provider includes from before that start is still stored. Once a cursor exists, a bar older than it is logged late and is not decided again.

Candles are parsed by the existing adapters. Nothing in this path copies a close forward or builds a gap bar. `include_empty_intervals` is not set. Two equal candles for one open time are stored once. Two different candles for one open time are both dropped.

## Closed bars

A bar is closed only when `open_time + bar_seconds <= now`. The clock is injectable. The forming bar is not stored and is not passed to the runner.

A synthetic bar, including a no-trade bar the historical cache may already hold, is not passed to the runner. In the window it is a hole: the series sample is empty and the bar object is absent, so it cannot produce a close or an OCO touch. A missing slot is also not a bar. The scheduler does not invent one.

## Scheduler

The worker is a thread started from the app lifespan and stopped on shutdown. The tick does not run on the request event loop, so `GET /health` stays fast while a tick is in progress.

`LIVE_FEED_ENABLED` turns it on (`1`, `true`, `yes`, `on`). Unset or any other value leaves it off. Tests leave it off. With no registrations the thread still starts when the flag is on, and it does not fetch until a version is registered.

Each tick, for each registered `(strategy id, version)`:

1. Fetch the majors in that strategy.
2. Walk closed open times after the cursor, in order.
3. Call `step` for a real closed bar that this version has not already decided.
4. Persist the decision and the new runner state in the same write as the cursor.

A missing open time is left alone until the following bar is already closed (`now >= open_time + 2 * bar_seconds`). A bar that arrives in that gap is still decided once. Once the following bar has closed and the slot is still empty, the slot is logged as missed and the cursor moves past it without calling `step`. A real bar that shows up after that is logged as late and is not decided. A real bar that is decided after the following bar has already closed is also logged as late, and it is still decided once. A second copy of a decided bar is logged as a duplicate and is not decided again.

Two versions do not share a cursor. The same open time is decided once per version.

## Rate limits

The adapters keep their pace: about 12 seconds between GeckoTerminal calls, and 1 second between Birdeye calls. HTTP 429, HTTP 5xx, timeouts, and retryable transport errors sleep a bounded backoff and try at most five times. The live opener passes an RNG. Each sleep is `backoff + rng() * backoff`, where `rng()` is a fraction from 0 to 1. A numeric `Retry-After` on HTTP 429 is used when it is longer. The sleep is capped at 120 seconds. Callers that omit the RNG, including the historical backfill, keep the unjittered backoff. Tests pass the RNG and the sleeper in, so the delays are fixed and no test waits on a wall clock.

## Purge

Fetched bars and raw response bodies use the bar cache. The TTL is 86400 seconds, the same 24-hour cap as the rest of the cache, and the same delete on open, read, write, and close. Recorded decisions and the missed/late log expire on that clock too, because a decision carries the bar close. The registration and the cursor do not. They are not a copy of the candles.

## Restart

The cursor is `(user, strategy id, version) -> (grid origin, last open time, runner state)` in `ohlcv.sqlite`. A new scheduler on that file loads the registrations and continues after `last open time`. A bar at or before that open time is not decided again. A bar that was fetched but not yet committed is still in the cache and is decided on the next tick. After the 24-hour purge the candles are gone; the cursor remains, so those open times are not decided again when a later fetch brings them back.
