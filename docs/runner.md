# Shared runner

`service/app/runner` decides one bar. The backtester applies the decision to a simulated book. A paper runner calls the same function once per bar, starting from `from app.runner import step` in a process that has not imported the backtester. The runner does not send an order, hold a wallet, or sign anything. It does not read a clock, draw a random number, open a file, or use the network.

## Interface

`config_from_document(document)` reads the signals, rules, actions, assets, and `warmup_bars` from a validated IR document.

`initial_state(asset_ids)` is empty cross and bars-since memory, in asset order.

`make_window(t, open_time, series, bars)` builds the input for bar `t`. Each series is closed at `t`: its length is `t + 1`. `bars` is the candle at `t` for each asset, or `None` when that slot is a hole. A longer series passed in is not readable past `t`.

`BookView` is what the decision may know about the book before the bar. `entries` is the open long's average fill. `ocos` is the attached pair. The runner does not change the view and does not update cash.

`step(config, state, window, view)` returns a `StepResult`:

- `state` is the memory for the next bar. The state that was passed in is unchanged.
- `decision.signals` is every `(signal, asset)` pair the rules use, from that pre-bar book.
- `decision.oco` is empty during warmup. Otherwise it is, in asset order, an `OcoDrop` when the pair's long is already flat, or an `OcoTouch` when this bar's own high and low touch a leg. A missing bar, a synthetic bar, and the bar that attached the pair are not touches. When both legs are touched, the stop wins. A stop that gaps through is priced at the open. A take-profit is priced at its limit.
- `decision.rules` is empty during warmup. Otherwise each true signal is one `RuleAttempt`, in IR order. A hole is `skip="missing_bar"`. A synthetic bar is `skip="synthetic_bar"`. A real bar carries that bar's close and no skip. Sizing, commission, and the fill itself stay with the caller.

`run(config, state, windows, views)` calls `step` once per window and threads the returned state forward. `views[i]` is the book before `windows[i]`.

## Invariants

**Purity.** Bars and state go in. A decision and a new state come out. The inputs are not mutated. The module has no mutable global, no I/O, and no network.

**Determinism.** The same config, state, window, and view always produce the same result. Nothing in the decision reads the wall clock or a random source. Asset order and rule order are the IR order, not hash order.

**No look-ahead.** The decision on bar `t` sees series values at indexes `0..t` and the candle at `t`. Index `t + 1` is not on the window: reading it raises `IndexError`. A series longer than `t + 1` is rejected. A later bar cannot change this bar's decision.

**Stepping equals batch.** Calling `step` once per bar, passing each returned state into the next call, returns the same results as `run` on those windows and views.

**Real bars only.** The runner does not invent candles. A simulated fill is the caller's job, and only an attempt with a close — a real, non-synthetic bar — is one. A synthetic bar and a hole are skips, not orders queued for a later bar.

## Cost

The series buffers are not copied. Each bar still builds a closed window over those buffers, checks that the window stops at that bar, and returns a new decision and a new memory object. That is the paper-runner interface.

On the example EMA-cross, remapped to `1m` and run for 3000 bars, three timed calls after one warmup took 0.054s on the engine from `e56685c` and 0.085s through this runner (about 58% slower). The PM dig of the same shape recorded 0.050s and 0.084s. A profile of the slower run put the extra time in `make_window`, the future-window check, and `step`'s decision and memory objects, not in a second copy of the bars. Removing those objects would change what `step` returns. The walk was left as it is, so those timings are the before and the after.
