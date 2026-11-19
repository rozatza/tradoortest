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

`user_id` is required by the model. Callers validate the IR with `validate_ir` before constructing a row. This repository does not open a database and does not create a users table.

Any later stored entity (a backtest run, a saved result) uses the same rule: a required `user_id` on the row, and no shared access list.

## Auth seam

`service/app/auth.py` exposes a FastAPI dependency:

```python
def get_current_user() -> CurrentUser:
    return CurrentUser(user_id="local-user")
```

`CurrentUserDep` is `Annotated[CurrentUser, Depends(get_current_user)]`. A route that needs the owner declares that parameter. The dependency takes no token and ignores any caller-supplied identity. The only user id it can return is `local-user`.

When multi-user auth exists, replace the body of `get_current_user`. Rows already have `user_id`, so queries can filter on the value the dependency returns. Do not add signup, password storage, roles, or share links in place of that replacement.

`GET /health` stays public and returns `{"status": "ok"}`. It does not accept or reveal a user.

## What this runtime does not include

- Accounts, sessions, invites, or access-control lists.
- Wallets, private keys, signing, transaction building, swaps, or order routing.
- The strategy-file parser, indicator math, market data, or a backtester. Expression text is parsed by `service/app/expr`.

`.env.example` has no values. Do not commit secrets.
