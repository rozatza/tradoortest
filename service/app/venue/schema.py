"""Strict parse of the pinned Swap API V2 bodies.

JSON numbers stay marked so a string cannot pass for a number, or the
other way around. Duplicate keys are rejected. A field that is not in
the pin is rejected. The provider body is not kept.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.venue.address import require_address
from app.venue.errors import VenueExpired, VenueRejected, VenueSchemaError
from app.venue.pin import (
    ACCOUNT_META_FIELDS,
    BLOCKHASH_FIELDS,
    BUILD_FIELDS,
    BUILD_REQUIRED,
    FETCHED_AT_FIELDS,
    INSTRUCTION_FIELDS,
    MODES,
    ORDER_FIELDS,
    ORDER_REQUIRED,
    PLATFORM_FEE_FIELDS,
    ROUTE_STEP_FIELDS,
    ROUTE_STEP_REQUIRED,
    ROUTERS,
    SWAP_INFO_FIELDS,
    SWAP_TYPES,
)
from app.venue.types import AccountRole, DecodedInstruction, VenueExpiry

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}$")
_DECIMAL = re.compile(r"^-?(?:0|[1-9]\d{0,19})(?:\.\d{1,12})?$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_U64 = re.compile(r"^(?:0|[1-9]\d{0,19})$")


class JsonInt(str):
    """A JSON integer. ``str`` so the digits are the ones on the wire."""


class JsonFloat(str):
    """A JSON number that was not an integer."""


@dataclass(frozen=True, slots=True)
class ParsedOrder:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    minimum_out_amount: int
    price_impact: str
    route_label: str
    router: str
    expiry: VenueExpiry
    request_id: str
    slippage_bps: int | None
    fee_bps: int | None
    fee_mint: str | None
    fee_amount: int | None
    swap_mode: str
    network_fee_lamports: int | None
    priority_fee_lamports: int | None
    rent_fee_lamports: int | None


@dataclass(frozen=True, slots=True)
class ParsedBuild:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    minimum_out_amount: int
    route_label: str
    instructions: tuple[DecodedInstruction, ...]
    lookup_tables: tuple[tuple[str, tuple[str, ...]], ...]
    recent_blockhash: bytes
    last_valid_block_height: int
    swap_mode: str


def loads(raw: bytes) -> object:
    """Parse JSON. Numbers keep their spelling. Duplicate keys fail."""
    try:
        return json.loads(
            raw,
            parse_int=JsonInt,
            parse_float=JsonFloat,
            object_pairs_hook=_pairs,
        )
    except VenueSchemaError:
        raise
    except ValueError:
        raise VenueSchemaError("not_json") from None


def parse_order(payload: object, now: int) -> ParsedOrder:
    """The quote shape. ``transaction`` must be null."""
    body = _object(payload, ORDER_FIELDS)
    _reject_errors(body)
    _require_route_key(body)
    _require(body, ORDER_REQUIRED)
    if body["transaction"] is not None:
        raise VenueRejected("unexpected_transaction")
    if "taker" in body and body["taker"] is not None:
        raise VenueSchemaError("unexpected_taker")
    input_mint = require_address(_text(body["inputMint"]))
    output_mint = require_address(_text(body["outputMint"]))
    if input_mint == output_mint:
        raise VenueRejected("mint_mismatch")
    in_amount = _positive_u64(_text(body["inAmount"]))
    out_amount = _positive_u64(_text(body["outAmount"]))
    minimum = _positive_u64(_text(body["otherAmountThreshold"]))
    if minimum > out_amount:
        raise VenueRejected("amount_mismatch")
    swap_mode = _exact_in(body["swapMode"])
    route_label = _route(body["routePlan"])
    router = _text(body["router"])
    if router not in ROUTERS:
        raise VenueSchemaError("missing_route")
    request_id = _text(body["requestId"])
    if not _REQUEST_ID.match(request_id):
        raise VenueSchemaError("bad_request_id")
    height = _maybe_text_height(body, "lastValidBlockHeight")
    expire_at = _maybe_expire(body, "expireAt")
    if height is None and expire_at is None:
        raise VenueSchemaError("missing_expiry")
    if expire_at is not None and now >= expire_at:
        raise VenueExpired("quote_expired")
    _optional_enums(body)
    _optional_numbers(body)
    slippage = _maybe_bps(body, "slippageBps")
    fee_bps, fee_mint, fee_amount = _fees(body)
    network_fee = _maybe_lamports(body, "signatureFeeLamports")
    priority_fee = _maybe_lamports(body, "prioritizationFeeLamports")
    rent_fee = _maybe_lamports(body, "rentFeeLamports")
    _maybe_address(body, "signatureFeePayer")
    _maybe_address(body, "prioritizationFeePayer")
    _maybe_address(body, "rentFeePayer")
    _maybe_address(body, "referralAccount")
    _maybe_address(body, "maker")
    _maybe_token(body, "quoteId")
    if "priceImpactPct" in body and body["priceImpactPct"] is not None:
        _decimal_text(_text(body["priceImpactPct"]))
    if "transactionVersion" in body and body["transactionVersion"] is not None:
        _version(body["transactionVersion"], allow_one=True)
    if "gasless" in body and type(body["gasless"]) is not bool:
        raise VenueSchemaError("bad_type")
    return ParsedOrder(
        input_mint=input_mint,
        output_mint=output_mint,
        in_amount=in_amount,
        out_amount=out_amount,
        minimum_out_amount=minimum,
        price_impact=_impact(body["priceImpact"]),
        route_label=route_label,
        router=router,
        expiry=VenueExpiry(height, expire_at),
        request_id=request_id,
        slippage_bps=slippage,
        fee_bps=fee_bps,
        fee_mint=fee_mint,
        fee_amount=fee_amount,
        swap_mode=swap_mode,
        network_fee_lamports=network_fee,
        priority_fee_lamports=priority_fee,
        rent_fee_lamports=rent_fee,
    )


def parse_build(payload: object) -> ParsedBuild:
    """Instructions, lookup tables, and the blockhash. No signed bytes."""
    body = _object(payload, BUILD_FIELDS)
    _require_route_key(body)
    _require(body, BUILD_REQUIRED)
    if _version(body["transactionVersion"], allow_one=False) != 0:
        raise VenueSchemaError("unsupported_version")
    if body.get("tipInstruction") is not None:
        raise VenueRejected("unexpected_tip")
    input_mint = require_address(_text(body["inputMint"]))
    output_mint = require_address(_text(body["outputMint"]))
    if input_mint == output_mint:
        raise VenueRejected("mint_mismatch")
    in_amount = _positive_u64(_text(body["inAmount"]))
    out_amount = _positive_u64(_text(body["outAmount"]))
    minimum = _positive_u64(_text(body["otherAmountThreshold"]))
    if minimum > out_amount:
        raise VenueRejected("amount_mismatch")
    swap_mode = _exact_in(body["swapMode"])
    route_label = _route(body["routePlan"])
    _maybe_bps(body, "slippageBps")
    if "priceImpactPct" in body and body["priceImpactPct"] is not None:
        _decimal_text(_text(body["priceImpactPct"]))
    if "computeUnitPrice" in body and body["computeUnitPrice"] is not None:
        _u64(_text(body["computeUnitPrice"]))
    instructions = (
        _instruction_list(body["computeBudgetInstructions"])
        + _instruction_list(body["setupInstructions"])
        + (_instruction(body["swapInstruction"]),)
        + _optional_instruction(body.get("cleanupInstruction"))
        + _instruction_list(body["otherInstructions"])
    )
    if not instructions:
        raise VenueSchemaError("bad_instructions")
    tables = _tables(body["addressesByLookupTableAddress"])
    blockhash, height = _blockhash(body["blockhashWithMetadata"])
    return ParsedBuild(
        input_mint=input_mint,
        output_mint=output_mint,
        in_amount=in_amount,
        out_amount=out_amount,
        minimum_out_amount=minimum,
        route_label=route_label,
        instructions=instructions,
        lookup_tables=tables,
        recent_blockhash=blockhash,
        last_valid_block_height=height,
        swap_mode=swap_mode,
    )


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise VenueSchemaError("duplicate_field")
    return dict(pairs)


def _object(value: object, allowed: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise VenueSchemaError("bad_object")
    if set(value) - allowed:
        raise VenueSchemaError("unknown_field")
    return value


def _require(body: dict[str, object], required: frozenset[str]) -> None:
    if required - set(body):
        raise VenueSchemaError("missing_field")


def _require_route_key(body: dict[str, object]) -> None:
    route = body.get("routePlan", None)
    if "routePlan" not in body or route is None or route == [] or not isinstance(route, list):
        raise VenueSchemaError("missing_route")


def _reject_errors(body: dict[str, object]) -> None:
    code = body.get("errorCode")
    message = body.get("errorMessage")
    error = body.get("error")
    if code is None and message is None and error is None:
        return
    router = body.get("router")
    name = router if type(router) is str and router in ROUTERS else "unknown"
    if type(code) is JsonInt and _U64.match(str(code)) and int(code) < 1_000_000:
        raise VenueRejected(f"router={name} error_code={int(code)}")
    raise VenueRejected("venue_error")


def _route(value: object) -> str:
    if not isinstance(value, list) or not value or len(value) > 8:
        raise VenueSchemaError("missing_route")
    labels: list[str] = []
    for step in value:
        body = _object(step, ROUTE_STEP_FIELDS)
        if ROUTE_STEP_REQUIRED - set(body):
            raise VenueSchemaError("missing_route")
        info = body["swapInfo"]
        if not isinstance(info, dict):
            raise VenueSchemaError("missing_route")
        if set(info) - SWAP_INFO_FIELDS:
            raise VenueSchemaError("unknown_field")
        if SWAP_INFO_FIELDS - set(info):
            raise VenueSchemaError("missing_route")
        label = info["label"]
        if type(label) is not str or not _LABEL.match(label):
            raise VenueSchemaError("missing_route")
        require_address(_text(info["ammKey"]))
        require_address(_text(info["inputMint"]))
        require_address(_text(info["outputMint"]))
        _positive_u64(_text(info["inAmount"]))
        _positive_u64(_text(info["outAmount"]))
        _bps(body["bps"])
        _percent(body["percent"])
        if "usdValue" in body and body["usdValue"] is not None:
            _number(body["usdValue"])
        labels.append(label)
    return ",".join(labels)


def _fees(body: dict[str, object]) -> tuple[int | None, str | None, int | None]:
    fee_bps = _maybe_bps(body, "feeBps")
    fee_mint = _maybe_address(body, "feeMint")
    fee_amount: int | None = None
    if "platformFee" not in body or body["platformFee"] is None:
        return fee_bps, fee_mint, fee_amount
    fee = _object(body["platformFee"], PLATFORM_FEE_FIELDS)
    if set(fee) != PLATFORM_FEE_FIELDS:
        raise VenueSchemaError("missing_field")
    amount = _u64(_text(fee["amount"]))
    bps = _bps(fee["feeBps"])
    mint = require_address(_text(fee["feeMint"]))
    if fee_bps is not None and fee_bps != bps:
        raise VenueSchemaError("bad_number")
    return bps, mint, amount


def _instruction_list(value: object) -> tuple[DecodedInstruction, ...]:
    if not isinstance(value, list) or len(value) > 16:
        raise VenueSchemaError("bad_instructions")
    return tuple(_instruction(item) for item in value)


def _optional_instruction(value: object) -> tuple[DecodedInstruction, ...]:
    if value is None:
        return ()
    return (_instruction(value),)


def _instruction(value: object) -> DecodedInstruction:
    body = _object(value, INSTRUCTION_FIELDS)
    if set(body) != INSTRUCTION_FIELDS:
        raise VenueSchemaError("missing_field")
    program = require_address(_text(body["programId"]))
    accounts = body["accounts"]
    if not isinstance(accounts, list) or len(accounts) > 64:
        raise VenueSchemaError("bad_accounts")
    roles: list[AccountRole] = []
    for account in accounts:
        meta = _object(account, ACCOUNT_META_FIELDS)
        if set(meta) != ACCOUNT_META_FIELDS:
            raise VenueSchemaError("missing_field")
        if type(meta["isSigner"]) is not bool or type(meta["isWritable"]) is not bool:
            raise VenueSchemaError("bad_accounts")
        roles.append(
            AccountRole(
                require_address(_text(meta["pubkey"])),
                meta["isSigner"],
                meta["isWritable"],
            )
        )
    data = body["data"]
    if type(data) is not str or len(data) > 1800:
        raise VenueSchemaError("bad_instruction_data")
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError:
        raise VenueSchemaError("bad_instruction_data") from None
    if len(raw) > 1232:
        raise VenueSchemaError("bad_instruction_data")
    return DecodedInstruction(program, tuple(roles), raw)


def _tables(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise VenueSchemaError("bad_lookup_table")
    tables: list[tuple[str, tuple[str, ...]]] = []
    for key, contents in value.items():
        address = require_address(key)
        if not isinstance(contents, list) or len(contents) > 256:
            raise VenueSchemaError("bad_lookup_table")
        tables.append((address, tuple(require_address(_text(item)) for item in contents)))
    return tuple(tables)


def _blockhash(value: object) -> tuple[bytes, int]:
    body = _object(value, BLOCKHASH_FIELDS)
    if "blockhash" not in body or "lastValidBlockHeight" not in body:
        raise VenueSchemaError("missing_field")
    raw = body["blockhash"]
    if not isinstance(raw, list) or len(raw) != 32:
        raise VenueSchemaError("bad_blockhash")
    out = bytearray()
    for item in raw:
        if type(item) is not JsonInt or not _U64.match(str(item)):
            raise VenueSchemaError("bad_blockhash")
        number = int(item)
        if number > 255:
            raise VenueSchemaError("bad_blockhash")
        out.append(number)
    if "fetchedAt" in body and body["fetchedAt"] is not None:
        fetched = _object(body["fetchedAt"], FETCHED_AT_FIELDS)
        if set(fetched) != FETCHED_AT_FIELDS:
            raise VenueSchemaError("missing_field")
        secs = fetched["secs_since_epoch"]
        if type(secs) is not JsonInt:
            raise VenueSchemaError("bad_number")
        _u64(str(secs))
        nanos = fetched["nanos_since_epoch"]
        if type(nanos) is not JsonInt or not _U64.match(str(nanos)) or int(nanos) > 1_000_000_000:
            raise VenueSchemaError("bad_number")
    return bytes(out), _json_height(body["lastValidBlockHeight"])


def _optional_enums(body: dict[str, object]) -> None:
    if "mode" in body and body["mode"] is not None and _text(body["mode"]) not in MODES:
        raise VenueSchemaError("bad_type")
    if (
        "swapType" in body
        and body["swapType"] is not None
        and _text(body["swapType"]) not in SWAP_TYPES
    ):
        raise VenueSchemaError("bad_type")


def _optional_numbers(body: dict[str, object]) -> None:
    for key in ("inUsdValue", "outUsdValue", "swapUsdValue", "totalTime"):
        if key in body and body[key] is not None:
            _number(body[key])


def _maybe_lamports(body: dict[str, object], key: str) -> int | None:
    if key not in body or body[key] is None:
        return None
    if type(body[key]) is not JsonInt:
        raise VenueSchemaError("bad_number")
    return _u64(str(body[key]))


def _maybe_bps(body: dict[str, object], key: str) -> int | None:
    if key not in body or body[key] is None:
        return None
    return _bps(body[key])


def _maybe_address(body: dict[str, object], key: str) -> str | None:
    if key not in body or body[key] is None:
        return None
    return require_address(_text(body[key]))


def _maybe_token(body: dict[str, object], key: str) -> None:
    if key not in body or body[key] is None:
        return
    if type(body[key]) is not str or not _REQUEST_ID.match(body[key]):
        raise VenueSchemaError("bad_type")


def _maybe_text_height(body: dict[str, object], key: str) -> int | None:
    if key not in body or body[key] is None:
        return None
    return _text_height(body[key])


def _maybe_expire(body: dict[str, object], key: str) -> int | None:
    if key not in body or body[key] is None:
        return None
    return _expire(body[key])


def _exact_in(value: object) -> str:
    if type(value) is not str or value != "ExactIn":
        raise VenueSchemaError("bad_swap_mode")
    return value


def _version(value: object, *, allow_one: bool) -> int:
    if type(value) is not JsonInt or str(value) not in ("0", "1"):
        raise VenueSchemaError("unsupported_version")
    number = int(value)
    if number == 1 and not allow_one:
        raise VenueSchemaError("unsupported_version")
    return number


def _text(value: object) -> str:
    if type(value) is not str:
        raise VenueSchemaError("bad_type")
    return value


def _impact(value: object) -> str:
    text = _number(value)
    number = Decimal(text)
    if number < Decimal("-1000") or number > Decimal("1000"):
        raise VenueSchemaError("bad_number")
    return text


def _percent(value: object) -> None:
    text = _number(value)
    number = Decimal(text)
    if number < 0 or number > 100:
        raise VenueSchemaError("bad_number")


def _decimal_text(value: str) -> str:
    if not _DECIMAL.match(value):
        raise VenueSchemaError("bad_number")
    number = Decimal(value)
    if not number.is_finite():
        raise VenueSchemaError("bad_number")
    return value


def _number(value: object) -> str:
    if type(value) not in (JsonInt, JsonFloat):
        raise VenueSchemaError("bad_number")
    text = str(value)
    if not _DECIMAL.match(text):
        raise VenueSchemaError("bad_number")
    return text


def _bps(value: object) -> int:
    if type(value) is not JsonInt or not _U64.match(str(value)):
        raise VenueSchemaError("bad_number")
    number = int(value)
    if number > 10_000:
        raise VenueSchemaError("bad_number")
    return number


def _positive_u64(value: object) -> int:
    number = _u64(value)
    if number <= 0:
        raise VenueRejected("amount_mismatch")
    return number


def _u64(value: object) -> int:
    if type(value) is not str or not _U64.match(value):
        raise VenueSchemaError("bad_amount")
    number = int(value)
    if number >= 2**64:
        raise VenueSchemaError("bad_amount")
    return number


def _text_height(value: object) -> int:
    if type(value) is not str or not _U64.match(value):
        raise VenueSchemaError("bad_block_height")
    return _height_range(int(value))


def _json_height(value: object) -> int:
    if type(value) is not JsonInt or not _U64.match(str(value)):
        raise VenueSchemaError("bad_block_height")
    return _height_range(int(value))


def _height_range(number: int) -> int:
    if number < 1 or number >= 2**63:
        raise VenueSchemaError("bad_block_height")
    return number


def _expire(value: object) -> int:
    if type(value) is not str or len(value) > 40 or value != value.strip():
        raise VenueSchemaError("bad_expiry")
    if _U64.match(value):
        number = int(value)
        if number >= 10**12:
            number //= 1000
        if number < 1_000_000_000 or number > 10_000_000_000:
            raise VenueSchemaError("bad_expiry")
        return number
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise VenueSchemaError("bad_expiry") from None
    if parsed.tzinfo is None:
        raise VenueSchemaError("bad_expiry")
    return int(parsed.timestamp())
