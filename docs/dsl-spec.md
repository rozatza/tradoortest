# Strategy DSL and JSON IR v1

This document is the contract for a strategy. The canonical artifact is JSON IR
validated by `schemas/strategy-ir-v1.schema.json`. A strategy file in the DSL
below is parsed by `service/app/dsl` into that IR. Expression text inside the
file is parsed by `service/app/expr`. The save envelope around a canonical IR
is `docs/save-format.md`.

## Scope

A strategy is **spot-only**, **long-only**, and **Solana-only**.

- `portfolio.mode` is `spot`. `assets[].market` is `spot`. Perpetuals, margin, and any other market type are rejected.
- `portfolio.direction` is `long_only`. Every action `side` is `long`. `short` and `sell_to_open` are rejected. An `exit` action closes a long; it does not open a short.
- `portfolio.chain` and `assets[].chain` are `solana`. No other chain or venue is representable.
- Leverage is not a field. An extra `leverage` property fails schema validation.
- The quote currency is `USDC`.

One user runs the tool. Ownership of a saved strategy is a storage concern, not an IR field. See `docs/design-notes.md`.

## Version

`ir_version` is required and is the string constant **`"1.0"`**.

The token `"1"` is a different value and does not validate. Bumping the IR is a new constant in a later schema, not a silent alias.

## Document shape

```text
strategy
engine
capital
costs
assets
derived_series
signals
actions
rules
portfolio
```

The IR object carries those keys plus `ir_version`. Unknown keys are rejected.

Identifiers (`strategy.id`, series, signals, actions, rules, asset ids) match `^[a-z][a-z0-9_]{0,63}$`.

## strategy

Names the strategy. The id is stable inside one document. The display name is a non-empty string.

```text
strategy rsi_cross_sol:
  name: "RSI cross"
  description: "Enter long when RSI crosses above 30."
```

```json
"strategy": { "id": "rsi_cross_sol", "name": "RSI cross", "description": "..." }
```

`description` may be omitted. The owner is not stored here.

## engine

How bars are sampled and when orders fill.

| Field | v1 value |
| --- | --- |
| `bar_size` | `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `8h`, `12h`, `1d` |
| `event` | `bar_close` only |
| `fill_price_mode` | `on_close` only |
| `warmup_bars` | integer `>= 0` |

```text
engine:
  bar_size: 1h
  event: bar_close
  fill_price_mode: on_close
  warmup_bars: 14
```

Evaluation uses only data through the close of the current bar. The fill price is that close, then costs are applied (see costs). Bars inside the warmup window do not fire rules. Indicator outputs are empty during their own warmup; this schema does not invent those values.

## capital

Starting quote balance for a run of the strategy.

| Field | Rule |
| --- | --- |
| `initial` | number, strictly greater than 0, at most `1e12` |
| `currency` | `USDC` |

Zero and negative balances are invalid. There is no borrowed cash and no leverage multiplier on this object.

```text
capital:
  initial: 10000 USDC
```

## costs

Per-fill costs in basis points of notional. Both are required and must be `>= 0` and `<= 10000`.

```text
costs:
  commission_bps: 10
  slippage_bps: 5
```

For a fill at bar close `P` with notional `N`:

- commission = `N * commission_bps / 10000`
- a buy fill price is `P * (1 + slippage_bps / 10000)`
- a sell fill price (closing a long) is `P * (1 - slippage_bps / 10000)`

## assets

The markets a strategy may trade. One to eight assets. Each asset is a Solana spot market quoted in USDC.

| Field | Rule |
| --- | --- |
| `id` | identifier used by actions |
| `symbol` | ticker matching `^[A-Z][A-Z0-9]{0,15}$` (for example `SOL`) |
| `chain` | `solana` |
| `market` | `spot` |
| `quote` | `USDC` |

```text
asset sol:
  symbol: SOL
  chain: solana
  market: spot
  quote: USDC
```

Mint addresses are not part of IR v1. A symbol is only a label; the chain constant is what keeps the market on Solana. `ETH` with `chain: ethereum` is invalid even if the ticker is a major.

Built-in series (`open`, `high`, `low`, `close`, `volume`) are that asset's bars. When an action runs, expressions are read against that action's asset. v1 does not qualify a series with an asset prefix.

## derived_series

Named indicator outputs. The array may be empty. A series id must not reuse a built-in name. `source` is a built-in series or a derived series declared earlier in the array.

`kind` is `indicator`. The indicator set is `sma`, `ema`, `rsi`, `atr`, `macd`, `bollinger`, `stochastic`.

| Indicator | Params | `output` |
| --- | --- | --- |
| `sma`, `ema`, `atr` | `period` integer 1–500 | omitted |
| `rsi` | `period` integer 2–500 | omitted |
| `macd` | `fast`, `slow`, `signal` integers 1–500 | `macd`, `signal`, or `histogram` |
| `bollinger` | `period` 1–500, `stddev` number in `(0, 10]` | `lower`, `middle`, or `upper` |
| `stochastic` | `k_period`, `d_period` integers 1–500 | `k` or `d` |

```text
series rsi_14 = RSI(close, period=14)
series ema_fast = EMA(close, period=12)
series bb_lower = BOLLINGER(close, period=20, stddev=2).lower
```

```json
{
  "id": "rsi_14",
  "kind": "indicator",
  "indicator": "rsi",
  "source": "close",
  "params": { "period": 14 }
}
```

Numeric indicator definitions are not part of this schema. Call names and parameters are fixed here so later code can implement them without renaming the IR.

## Expressions

Signals carry a boolean expression in canonical IR form. `service/app/expr` parses expression text and `to_ir` emits this tree. Operators and calls:

| IR `op` | Arity | DSL text |
| --- | --- | --- |
| `lit` | `value` number or boolean | `30`, `true` |
| `ref` | `name` | `rsi_14`, `close` |
| `neg`, `not` | `arg` | `-x`, `NOT x` |
| `add` `sub` `mul` `div` | `left`, `right` | `+ - * /` |
| `gt` `gte` `lt` `lte` `eq` `neq` | `left`, `right` | `> >= < <= == !=` |
| `and` `or` | `left`, `right` | `AND`, `OR` |
| `cross_above` | 2 args | `CROSS_ABOVE(a, b)` |
| `cross_below` | 2 args | `CROSS_BELOW(a, b)` |
| `bars_since` | 1 arg | `BARS_SINCE(cond)` |
| `entry_price` | 0 args | `ENTRY_PRICE()` |

`CROSS_ABOVE(a, b)` is true on the bar where `a` moves from `<= b` to `> b`. `CROSS_BELOW` is the opposite. `BARS_SINCE(cond)` is the number of bars since `cond` was last true, or empty if it has never been true. `ENTRY_PRICE()` is the fill price of the open long, and empty when flat.

Text grammar:

```text
expr     := or_expr
or_expr  := and_expr ("OR" and_expr)*
and_expr := not_expr ("AND" not_expr)*
not_expr := "NOT" not_expr | cmp_expr
cmp_expr := add_expr (cmp_op add_expr)?
cmp_op   := ">" | ">=" | "<" | "<=" | "==" | "!="
add_expr := mul_expr (("+" | "-") mul_expr)*
mul_expr := unary (("*" | "/") unary)*
unary    := "-" unary | primary
primary  := number | "true" | "false" | ident | call | "(" expr ")"
call     := ident "(" args? ")" ("." ident)?
args     := arg ("," arg)*
arg      := ident "=" expr | expr
```

`AND`, `OR`, and `NOT` associate as written above: `OR` is loosest, then `AND`, then `NOT`. `+` and `-` are left-associative, as are `*` and `/`. Unary `-` is right-associative and binds tighter than `*`. A comparison is not associative: `a > b > c` is an error. `NOT a > b` is `NOT (a > b)` because a comparison binds tighter than `NOT`.

A `ref` name must be a built-in series, a derived series, or an earlier signal id. Indicator calls belong in `derived_series`, not inside a signal expression. The parser still accepts an indicator call, so a derived-series formula can be checked, but `to_ir` rejects it: a signal expression has no indicator operator.

### Parser

`service/app/expr` is the expression parser. It does not evaluate expressions. There is no `eval`, `exec`, or `compile`.

| Entry point | Result |
| --- | --- |
| `parse(source, names=None)` | any expression |
| `parse_condition(source, names=None)` | expression whose type is `BOOLEAN` |
| `parse_numeric(source, names=None)` | expression whose type is `NUMERIC` |
| `to_ir(node)` | canonical signal IR, or an error for an indicator call |
| `from_ir(tree, names=None, path="$")` / `format_expr(node)` | the inverse direction for canonical ops |

`names`, when passed, maps an identifier to `NUMERIC` or `BOOLEAN`. Built-in series are `NUMERIC` unless the map overrides them. The strategy parser passes this map while it parses, so a signal id is `BOOLEAN` and a series id is `NUMERIC` before the surrounding operator is type-checked. Omit `names` and every bare identifier is `NUMERIC`.

Every error includes a 0-based `offset` into the source string and a 1-based `line` and `column`. A tab counts as one column. `str(error)` is `line:column: message (offset N)`.

The following points are fixed here because the grammar above does not state them.

**Layout.** Spaces, tabs, and newlines separate tokens. `#` comments run to the end of the line. Operators are single tokens (`>=`, not `> =`). There is no unary `+`, no `**`, no string literal, and no trailing comma.

**Numbers.** A number is digits, optionally `.` and more digits, optionally an exponent `[eE][+-]?` digits. The token must start with a digit (`.5` is not a number). No decimal point and no exponent means an integer literal, so `30` stays `30` in IR. A decimal or an exponent is a float, including `1e2`. A non-finite value is a bad number. A literal with more than 1000 digits (`MAX_NUMBER_LITERAL_DIGITS`) is a bad number at the start of the token; the parser does not call `int` on it. Unary minus is the `neg` operator, not part of the literal: `-30` is `{"op":"neg","arg":{"op":"lit","value":30}}`.

**Names.** A series reference matches `^[a-z][a-z0-9_]{0,63}$`. `AND`, `OR`, `NOT`, `true`, and `false` are reserved and case-insensitive. Call names are case-insensitive: `CROSS_ABOVE`, `CROSS_BELOW`, `BARS_SINCE`, `ENTRY_PRICE`, and the indicator names below. `bb` is an alias of `bollinger`. A known call name written in identifier form without `(` is a reference (`cross_above`); the uppercase spelling without `(` is an error because it is not an identifier.

**Calls.** `CROSS_ABOVE` and `CROSS_BELOW` take two positional arguments. `BARS_SINCE` takes one. `ENTRY_PRICE` takes none. Keyword arguments are only for indicators. Indicator parameters may be positional after the source or `name=expr`, and keyword names are case-insensitive. The canonical parameter order is stored on the AST.

| Indicator | Arguments after `source` | Output suffix |
| --- | --- | --- |
| `sma`, `ema`, `atr`, `rsi` | `period` | none |
| `macd` | `fast`, `slow`, `signal` | `.macd`, `.signal`, or `.histogram` |
| `bollinger` (`bb`) | `period`, `stddev` | `.lower`, `.middle`, or `.upper` |
| `stochastic` | `k_period`, `d_period` | `.k` or `.d` |

The output suffix is part of the call (`BOLLINGER(close, period=20, stddev=2).lower`), not of a surrounding parenthesized group. Output names are case-insensitive and stored in lowercase. `macd`, `bollinger`, and `stochastic` require an output. The others reject one. Period ranges (`1–500` and the rest of the indicator table above) are checked when a derived series is validated, not by this parser. Argument expressions must be numeric; they are not required to be literals.

**Limits.** `MAX_EXPR_DEPTH` is 80. The expression parser counts grouping parentheses, calls, `NOT`, and unary minus. One past that limit is `expr_too_deep`, positioned at the token that crossed it. `from_ir` counts every nested IR node and fails at the same limit, with `path` set to that node. `parse_strategy` uses that node count on every signal formula and series formula, and raises `expr_too_deep` at the start of the formula. A file it accepts is shallow enough for `validate_ir`, `canonicalize`, and `format_strategy`. `MAX_EXPR_SOURCE_LENGTH` is 10000 code points. A longer source is `expression_too_long` at that offset.

**Types.** Each node is `NUMERIC` or `BOOLEAN`. When `names` is passed, a mapped signal id is `BOOLEAN` and a mapped or built-in series id is `NUMERIC`. Those types are applied as the node is built.

| Form | Type | Rule |
| --- | --- | --- |
| number | `NUMERIC` | |
| `true`, `false` | `BOOLEAN` | |
| identifier | from `names`, or `NUMERIC` | with `names`, an unknown name is `unknown_series`; without `names`, a bare name is `NUMERIC` |
| `-x`, `+ - * /` | `NUMERIC` | each operand is `NUMERIC` |
| comparison | `BOOLEAN` | both operands are `NUMERIC`, including `==` and `!=` |
| `NOT`, `AND`, `OR` | `BOOLEAN` | each operand is `BOOLEAN` |
| `CROSS_ABOVE`, `CROSS_BELOW` | `BOOLEAN` | both arguments are `NUMERIC` |
| `BARS_SINCE` | `NUMERIC` | the argument is `BOOLEAN` |
| `ENTRY_PRICE` | `NUMERIC` | |
| indicator call | `NUMERIC` | every argument is `NUMERIC` |

With no name map, a bare identifier is `NUMERIC`. With a name map, resolution happens in the parse (and in `from_ir`), not as a second walk that retypes an all-numeric tree. `rsi_buy AND x > 1`, `NOT rsi_buy`, and `BARS_SINCE(rsi_buy)` parse when `rsi_buy` is `BOOLEAN` and `x` is `NUMERIC`. Using a signal as a number, or a series as a condition, is a positioned `type_error`. A name that is neither a built-in series nor in the map is `unknown_series`. Indicator parameter ranges are still checked when a derived series is validated, not by this parser.

**Errors.** Messages name the problem and carry a position: `empty input`, `unexpected token`, `unexpected character`, `unterminated paren`, `unknown function`, `wrong arity`, `trailing input`, `bad number`, `invalid identifier`, `type error`, `unknown series reference`, `expression nested too deeply`, and `expression exceeds length limit`. A chained comparison is reported as an unexpected token. `from_ir` sets `error.path` to a JSONPath such as `$.signals[1].expr.args[0]`. `str(error)` stays `line:column: message (offset N)`.

## signals

A signal has an id and an `expr`. Ids are unique. At least one signal is required.

```text
signal rsi_buy = CROSS_ABOVE(rsi_14, 30)
signal rsi_sell = CROSS_BELOW(rsi_14, 70)
```

```json
{
  "id": "rsi_buy",
  "expr": {
    "op": "cross_above",
    "args": [
      { "op": "ref", "name": "rsi_14" },
      { "op": "lit", "value": 30 }
    ]
  }
}
```

Comparisons involving an empty warmup value are false.

## actions

An action is `enter` or `exit`. Both require `side: long`, an `asset` id that exists, and `order.type: market`. Fills use the engine's `on_close` mode.

### Sizing

| Action | `sizing.mode` | `percent` |
| --- | --- | --- |
| `enter` | `percent_equity` | `(0, 100]` of current equity, in quote |
| `exit` | `percent_position` | `(0, 100]` of the open long |

```text
action enter_sol:
  type: enter
  side: long
  asset: sol
  order: market
  sizing: 25% equity
```

### Risk: take-profit / stop-loss as one OCO pair

Every `enter` action requires a `risk` object. An `exit` action must not carry `risk`.

`risk` is a single OCO pair. Both legs are required. Omitting `take_profit` or `stop_loss` is invalid.

| Field | v1 value |
| --- | --- |
| `type` | `oco` |
| `evaluation` | `on_entry_fill` |
| `movement` | `tighten_only` |
| `take_profit` | `{ "mode": "percent", "percent": P }` with `P` in `(0, 100]` |
| `stop_loss` | `{ "mode": "percent", "percent": P }` with `P` in `(0, 100)` |

Levels are anchored to the entry fill price `F` (after slippage):

- take-profit price = `F * (1 + take_profit.percent / 100)`
- stop-loss price = `F * (1 - stop_loss.percent / 100)`

The pair is attached when the entry fills. Cancelling one leg cancels the other. `movement: tighten_only` means a later change may only move the stop up or the target down, closer to price. A change that widens the stop or pushes the target further away is rejected. v1 documents store the initial percents; they cannot select another movement policy.

#### Intrabar OCO resolution

On a long, if one bar's low is at or below the stop and the same bar's high is at or above the take-profit, the stop fills and the take-profit is cancelled. The stop wins because that is the conservative resolution when the path inside the bar is unknown.

```text
  risk oco:
    evaluation: on_entry_fill
    movement: tighten_only
    take_profit: 6%
    stop_loss: 3%
```

## rules

A rule binds one signal to one action. At least one rule is required. `when` must name a signal in this document. `do` must name an action. A missing name is a semantic error (`unknown_signal` or `unknown_action`), not a silent no-op.

```text
rule enter_on_rsi_buy:
  when rsi_buy do enter_sol
rule exit_on_rsi_sell:
  when rsi_sell do exit_sol
```

On each `bar_close` after warmup, rules run from the top of the array downward. Each true signal invokes its action once. If `allow_pyramiding` is false and a long in that asset is already open, a further `enter` for that asset is skipped.

## portfolio

The book the strategy is allowed to hold.

| Field | v1 value |
| --- | --- |
| `mode` | `spot` |
| `direction` | `long_only` |
| `chain` | `solana` |
| `max_positions` | integer 1–20 |
| `allow_pyramiding` | boolean |
| `quote_currency` | `USDC` |

```text
portfolio:
  mode: spot
  direction: long_only
  chain: solana
  max_positions: 1
  allow_pyramiding: false
  quote_currency: USDC
```

`max_positions` counts distinct assets with an open long. It does not allow shorts.

## Strategy file

`parse_strategy` reads the text form and returns canonical IR with `ir_version` `"1.0"`. `format_strategy` prints that IR. Formatting the parse of that text is the same string. The three examples in `schemas/examples/` round-trip this way.

Top-level declarations may appear in any order. Blank lines and `#` comments (outside quoted strings) are ignored. After the comment is removed, all Unicode whitespace at the end of the line is stripped, including a space before `#`. Indentation is spaces, in multiples of 2. The canonical text uses one blank line between sections and this order: strategy, engine, capital, costs, assets, derived series, signals, actions, rules, portfolio.

```text
strategy rsi_cross_sol:
  name: "RSI cross"
  description: "Enter long when RSI crosses above 30."

engine:
  bar_size: 1h
  event: bar_close
  fill_price_mode: on_close
  warmup_bars: 14

capital:
  initial: 10000 USDC

costs:
  commission_bps: 10
  slippage_bps: 5

asset sol:
  symbol: SOL
  chain: solana
  market: spot
  quote: USDC

series rsi_14 = RSI(close, period=14)
series bb_lower = BOLLINGER(close, period=20, stddev=2).lower

signal rsi_buy = CROSS_ABOVE(rsi_14, 30)
signal both = rsi_buy AND close > 1

action enter_sol:
  type: enter
  side: long
  asset: sol
  order: market
  sizing: 25% equity
  risk oco:
    evaluation: on_entry_fill
    movement: tighten_only
    take_profit: 6%
    stop_loss: 3%

action exit_sol:
  type: exit
  side: long
  asset: sol
  order: market
  sizing: 100% position

rule enter_on_rsi_buy:
  when rsi_buy do enter_sol

portfolio:
  mode: spot
  direction: long_only
  chain: solana
  max_positions: 1
  allow_pyramiding: false
  quote_currency: USDC
```

`name` and `description` are JSON strings. A series formula is one indicator call: the source is a series name, and each parameter is a numeric literal. A signal formula is one boolean expression on that same line. Names already declared are in scope: built-ins and earlier series are `NUMERIC`, earlier signals are `BOOLEAN`. A series id may not be a built-in (`reserved_series`). A signal id may not reuse a built-in or an earlier series or signal id (`duplicate_id`).

`sizing: 25% equity` is `percent_equity`. `sizing: 100% position` is `percent_position`. An enter action's `risk oco` block is the OCO pair. An exit action omits it.

File errors use `StrategyError`. `code` is the expression code when the failure is inside a formula (`type_error`, `unknown_series`, `expr_too_deep`, `bad_number`, `invalid_expr`, and the other expression errors), or `syntax`, `missing_section`, `missing_field`, `unknown_field`, `duplicate_section`, `duplicate_field`, `duplicate_id`, `reserved_series`, or `invalid_series`. The offset is into the strategy file.

## Canonical IR and hash

`canonicalize` rewrites a document into one key order and one number spelling. Object keys follow the order in this spec (document keys, then each object's fields, then expression keys `op` then `value` / `name` / `arg` / `left`+`right` / `args`). Indicator `params` use the parameter order in the indicator table. A finite float that is an integer inside the safe integer range is stored as an integer, so `25` and `25.0` match. Other floats keep their value.

`canonical_json` is that object encoded as UTF-8 JSON with no insignificant whitespace. `ir_hash` is the lowercase sha256 hex digest of those bytes. Permuting keys, or writing an integral number as a float, does not change the hash. The same input always yields the same hash.

## Validation

`service/app/ir/validate.py` loads the draft 2020-12 schema and then checks references and expression types.

1. Schema failures return before semantic checks. Each issue has a JSONPath `path`, a `keyword`, and a `message`.
2. For a missing property, `path` points at the missing field (for example `$.ir_version` or `$.actions[0].risk.stop_loss`) and `keyword` is `required`.
3. Some schema failures are reported under a specific code instead of the generic keyword. Every other schema failure keeps its JSON Schema keyword (`const`, `required`, `additionalProperties`, `exclusiveMinimum`, and the rest).

| Code | When |
| --- | --- |
| `invalid_bar_size` | `engine.bar_size` is outside `1m`–`1d` |
| `invalid_sizing` | a sizing percent is not in `(0, 100]` |
| `insane_stop_loss` | stop-loss percent is `<= 0` or `>= 100`. A stop of 100% prices a long at zero, which is not a long protective stop |
| `insane_take_profit` | take-profit percent is `<= 0` or `> 100`. A long target has to stay above the entry |
| `unknown_signal` | `rules[].when` is not a signal id in this document |
| `unknown_action` | `rules[].do` is not an action id |
| `unknown_asset` | `actions[].asset` is not an asset id |
| `unknown_series` | a derived-series `source`, or an expression `ref`, is not a built-in series, an earlier derived series, or (inside a signal) an earlier signal |
| `duplicate_id` | the same id appears twice in one list, or a signal id reuses a series id |
| `duplicate_symbol` | two assets share the same `symbol` |
| `reserved_series` | a derived series id is `open`, `high`, `low`, `close`, or `volume` |
| `missing_entry` | no rule invokes an action whose `type` is `enter` |
| `type_error` | a signal expression is not `BOOLEAN`, or an operator's operand has the wrong type. Series names are `NUMERIC`. Earlier signal names are `BOOLEAN`. The check runs inside `from_ir` with that map |
| `expr_too_deep` | a signal expression is nested deeper than 80 IR nodes. `from_ir` reports it, and `parse_strategy` rejects the formula at its first character |
| `invalid_expr` | an IR expression is not an object, uses an unknown `op`, or is missing a child or a literal. `from_ir` raises it and `validate_ir` reports it. A signal formula that contains an indicator call is the same code: the indicator belongs in a `series` line. `parse_strategy` reports it at that call in the file |
| `expression_too_long` | a formula is longer than 10000 code points. The expression parser and `parse_strategy` raise this. It is not a code on an IR document |

`format_issues` renders one line per issue: `path: message [keyword]`.

## Worked examples

These three documents validate against the schema:

| Strategy | File | Idea |
| --- | --- | --- |
| RSI cross | `schemas/examples/rsi-cross.json` | `CROSS_ABOVE(rsi_14, 30)` enters; `CROSS_BELOW(rsi_14, 70)` exits |
| EMA cross | `schemas/examples/ema-cross.json` | fast EMA 12 crosses slow EMA 26 |
| Bollinger revert | `schemas/examples/bollinger-revert.json` | close crosses below the lower band, exits on a cross above the middle band |

Each example is SOL spot on Solana, long-only, with an OCO take-profit and stop-loss on the entry.

## Non-goals

This spec does not define indicator math, historical data, a backtester, or any order route. It does not define wallets, keys, signing, or swaps. The strategy file parser is `service/app/dsl`. Expression text is parsed by `service/app/expr`. The versioned save envelope is `docs/save-format.md`.
