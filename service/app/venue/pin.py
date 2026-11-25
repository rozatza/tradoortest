"""Pinned Jupiter Swap API V2.

Verified 2026-11-25T03:16:55Z by reading the public docs. ``dev.jup.ag``
served these pages. The documentation index also names
``developers.jup.ag``. Both describe the same API.

- https://dev.jup.ag/docs/swap
- https://dev.jup.ag/docs/api-reference/swap/order
- https://dev.jup.ag/docs/api-reference/swap/build
- https://dev.jup.ag/docs/api-reference/swap/execute
- https://dev.jup.ag/llms.txt
- https://developers.jup.ag/docs/swap/order-and-execute.md
- https://developers.jup.ag/docs/swap/build/index.md

OpenAPI ``3.0.3``, ``info.title`` ``Swap API V2``, ``info.version``
``2.0.0``, server ``https://api.jup.ag/swap/v2``.

``GET /order`` without ``taker`` is the quote. ``transaction`` is null.
``GET /build`` is the unsigned build. It returns instructions and
``addressesByLookupTableAddress``. ``POST /execute`` lands a signed
``/order`` transaction. This package does not call it.

A key is optional. Keyless quotes are documented. When a key is set it
is the ``x-api-key`` header only.
"""

from __future__ import annotations

JUPITER_API_TITLE = "Swap API V2"
JUPITER_API_VERSION = "2.0.0"
JUPITER_OPENAPI_VERSION = "3.0.3"
JUPITER_BASE_URL = "https://api.jup.ag/swap/v2"
JUPITER_ORDER_PATH = "/order"
JUPITER_BUILD_PATH = "/build"
# Present on the pinned API. Not a path this package requests.
JUPITER_EXECUTE_PATH = "/execute"
JUPITER_API_KEY_HEADER = "x-api-key"
JUPITER_API_KEY_ENV = "JUPITER_API_KEY"
JUPITER_VERIFIED_AT = "2026-11-25T03:16:55Z"

# Documented /order response fields. Anything else is a schema failure.
ORDER_FIELDS = frozenset(
    {
        "mode",
        "inputMint",
        "outputMint",
        "inAmount",
        "outAmount",
        "inUsdValue",
        "outUsdValue",
        "priceImpact",
        "swapUsdValue",
        "otherAmountThreshold",
        "swapMode",
        "slippageBps",
        "priceImpactPct",
        "routePlan",
        "referralAccount",
        "feeMint",
        "feeBps",
        "platformFee",
        "signatureFeeLamports",
        "signatureFeePayer",
        "prioritizationFeeLamports",
        "prioritizationFeePayer",
        "rentFeeLamports",
        "rentFeePayer",
        "swapType",
        "router",
        "transactionVersion",
        "transaction",
        "lastValidBlockHeight",
        "gasless",
        "requestId",
        "totalTime",
        "taker",
        "quoteId",
        "maker",
        "expireAt",
        "errorCode",
        "errorMessage",
        "error",
    }
)
ORDER_REQUIRED = frozenset(
    {
        "inputMint",
        "outputMint",
        "inAmount",
        "outAmount",
        "otherAmountThreshold",
        "priceImpact",
        "swapMode",
        "routePlan",
        "router",
        "transaction",
        "requestId",
    }
)

# Documented /build response fields.
BUILD_FIELDS = frozenset(
    {
        "inputMint",
        "outputMint",
        "inAmount",
        "outAmount",
        "otherAmountThreshold",
        "swapMode",
        "slippageBps",
        "priceImpactPct",
        "routePlan",
        "computeBudgetInstructions",
        "setupInstructions",
        "swapInstruction",
        "cleanupInstruction",
        "otherInstructions",
        "tipInstruction",
        "addressesByLookupTableAddress",
        "transactionVersion",
        "computeUnitPrice",
        "blockhashWithMetadata",
    }
)
BUILD_REQUIRED = frozenset(
    {
        "inputMint",
        "outputMint",
        "inAmount",
        "outAmount",
        "otherAmountThreshold",
        "swapMode",
        "routePlan",
        "computeBudgetInstructions",
        "setupInstructions",
        "swapInstruction",
        "otherInstructions",
        "addressesByLookupTableAddress",
        "transactionVersion",
        "blockhashWithMetadata",
    }
)

ROUTE_STEP_FIELDS = frozenset({"swapInfo", "percent", "bps", "usdValue"})
ROUTE_STEP_REQUIRED = frozenset({"swapInfo", "percent", "bps"})
SWAP_INFO_FIELDS = frozenset(
    {"ammKey", "label", "inputMint", "outputMint", "inAmount", "outAmount"}
)
PLATFORM_FEE_FIELDS = frozenset({"amount", "feeBps", "feeMint"})
INSTRUCTION_FIELDS = frozenset({"programId", "accounts", "data"})
ACCOUNT_META_FIELDS = frozenset({"pubkey", "isWritable", "isSigner"})
BLOCKHASH_FIELDS = frozenset({"blockhash", "lastValidBlockHeight", "fetchedAt"})
FETCHED_AT_FIELDS = frozenset({"secs_since_epoch", "nanos_since_epoch"})

ROUTERS = frozenset({"metis", "jupiterz", "dflow", "okx"})
MODES = frozenset({"ultra", "manual"})
SWAP_TYPES = frozenset({"aggregator", "rfq", "aggregator+rfq", "dflow", "okx"})

# Required query keys. ``slippageBps`` is optional and, when the caller sets
# it, is sent on both ``/order`` and ``/build``. It is a request parameter.
# The risk engine owns the cap (P4-04).
ORDER_QUERY_KEYS = frozenset({"inputMint", "outputMint", "amount", "swapMode"})
BUILD_QUERY_KEYS = frozenset(
    {"inputMint", "outputMint", "amount", "taker", "transactionVersion"}
)
SLIPPAGE_QUERY_KEY = "slippageBps"

# The build guide resolves lookup tables from the response. No RPC.
TRANSACTION_VERSION = 0
MAX_RESPONSE_BYTES = 65_536
QUOTE_TTL_SECONDS = 24 * 60 * 60
