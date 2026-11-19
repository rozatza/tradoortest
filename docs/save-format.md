# Strategy save format

A saved strategy is a JSON object wrapped around canonical IR v1. The IR itself is defined in `docs/dsl-spec.md` and checked by `service/app/ir/validate.py`. This envelope is what `save_document` / `load_document` in `service/app/ir/save.py` read and write.

The envelope is not the stored row. `user_id` lives on `StoredStrategy` and is not a field here, so the hash does not change with the owner. See `docs/design-notes.md`.

## Document

| Field | Value |
| --- | --- |
| `format` | the string `tradoor.strategy` |
| `format_version` | the string `1` |
| `ir_version` | copied from the IR. For this format it is the string `1.0` |
| `ir_hash` | lowercase sha256 hex of the canonical IR JSON |
| `ir` | the strategy IR object |

Key order in the file `save_document` writes is `format`, `format_version`, `ir_version`, `ir_hash`, `ir`. `load_document` accepts any key order.

```json
{
  "format": "tradoor.strategy",
  "format_version": "1",
  "ir_version": "1.0",
  "ir_hash": "<64 lowercase hex chars>",
  "ir": { "ir_version": "1.0" }
}
```

`ir` is stored in canonical form: the key order and number spelling from `docs/dsl-spec.md`. `ir_hash` is `ir_hash(ir)` over that canonical JSON (UTF-8, no insignificant whitespace). A file whose `ir` object uses a different key order still loads when the digest matches the canonical bytes.

## Checks

`load_document` rejects a document before returning the canonical IR.

| Code | When |
| --- | --- |
| `unsupported_format` | `format` is not `tradoor.strategy`, or `format_version` is not `1` |
| `unsupported_ir_version` | `ir.ir_version` is not `1.0` |
| `ir_version_mismatch` | the envelope `ir_version` is not the IR's `ir_version` |
| `ir_hash_mismatch` | `ir_hash` is not the sha256 of the canonical IR |
| `invalid_save` | the value is not an object, or `ir` is missing |

`1` and `1.0` are different version tokens, matching the IR rule. A later IR constant needs a new `format_version`. This format does not carry wallets, keys, signatures, or orders.
