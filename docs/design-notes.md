# Design notes

## Single-user runtime

tradoortest is an internal tool for one person. The process serves that person and nobody else. There is no signup, no user table, no role, and no sharing.

The data model still records an owner on every stored entity so a later multi-user deployment can fill the same field from a real session. That later work is not started here.

## Stored entities

The canonical IR (`ir_version` `"1.0"`) is the strategy itself. It has no `user_id`. Putting the owner inside the IR would change the document hash when the same strategy is saved by someone else.

A stored row wraps the IR. `StoredStrategy` in `service/app/models.py` is that envelope:

| Field | Meaning |
| --- | --- |
| `user_id` | Owner. Required. |
| `strategy_id` | `strategy.id` from the IR. |
| `ir_version` | Copied from the IR (`"1.0"`). |
| `ir` | The validated IR object. |

`user_id` is required by the model. Callers validate the IR with `validate_ir` before constructing a row. There is no users table.

A backtest job uses the same rule. The row in `data/backtest-jobs.sqlite` has a required `user_id`, and a read filters on the id `get_current_user` returned. See `docs/jobs.md`.

Any later stored entity uses the same rule: a required `user_id` on the row, and no shared access list.

## Auth seam

`service/app/auth.py` exposes a FastAPI dependency:

```python
def get_current_user() -> CurrentUser:
    return CurrentUser(user_id="local-user")
```

`CurrentUserDep` is `Annotated[CurrentUser, Depends(get_current_user)]`. A route that needs the owner declares that parameter. The dependency takes no token and ignores any caller-supplied identity. The only user id it can return is `local-user`.

When multi-user auth exists, replace the body of `get_current_user`. Rows already have `user_id`, so queries can filter on the value the dependency returns. Do not add signup, password storage, roles, or share links in place of that replacement.

`GET /health` stays public and returns `{"status": "ok"}`. It does not accept or reveal a user.

The agent tools use the same seam. `POST /tools/{tool_name}` passes `CurrentUser` into the dispatcher, and every store or job call uses `user.user_id`. Arguments, headers, and query parameters cannot set the owner. A row for any other id is `not_found`. See `docs/agent-tools.md`.

The author loop uses that same dependency. `data/agent.sqlite` stores sessions, messages, events, proposals, and validated hashes. Every table has `user_id`. There is still no users table. A session or proposal for any other id is HTTP 404. See `docs/agent-loop.md`.

## What this runtime does not include

- Accounts, sessions, invites, or access-control lists.
- Wallets, private keys, signing, transaction building, swaps, or order routing.
- A live order route. The backtest in `service/app/backtest` (`docs/backtest.md`) only simulates fills. Indicator math is `service/app/indicators` (`docs/dsl-spec.md`). Historical bars are ingested by `service/app/ohlcv` (`docs/ohlcv.md`) into a local store keyed with `user_id`. Strategy files are parsed by `service/app/dsl`. Expression text is parsed by `service/app/expr`. A saved file uses the envelope in `docs/save-format.md`; `user_id` stays on the row and is not part of that envelope.

`.env.example` has no values. Do not commit secrets.
