"""Immutable tool catalog, in the order the agent contract lists them."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.auth import CurrentUser
from app.tools.assets import search_asset
from app.tools.backtests import backtest_get, backtest_start, backtest_status
from app.tools.directives import add_chart, add_indicator
from app.tools.reference import read_authoring_reference
from app.tools.runtime import ToolRuntime
from app.tools.specs import DESCRIPTIONS, INPUTS, MUTATES, OUTPUTS
from app.tools.strategy_tools import update_strategy, validate, write_strategy

Handler = Callable[[Mapping[str, Any], CurrentUser, ToolRuntime], dict[str, Any]]

TOOL_ORDER = (
    "search_asset",
    "read_authoring_reference",
    "write_strategy",
    "update_strategy",
    "validate",
    "backtest_start",
    "backtest_status",
    "backtest_get",
    "add_chart",
    "add_indicator",
)

_HANDLERS: dict[str, Handler] = {
    "search_asset": search_asset,
    "read_authoring_reference": read_authoring_reference,
    "write_strategy": write_strategy,
    "update_strategy": update_strategy,
    "validate": validate,
    "backtest_start": backtest_start,
    "backtest_status": backtest_status,
    "backtest_get": backtest_get,
    "add_chart": add_chart,
    "add_indicator": add_indicator,
}


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool. The schemas are the contract the dispatcher enforces."""

    name: str
    description: str
    mutates_state: bool
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    handler: Handler


def _build() -> tuple[ToolSpec, ...]:
    specs: list[ToolSpec] = []
    for name in TOOL_ORDER:
        specs.append(
            ToolSpec(
                name=name,
                description=DESCRIPTIONS[name],
                mutates_state=MUTATES[name],
                input_schema=INPUTS[name],
                output_schema=OUTPUTS[name],
                handler=_HANDLERS[name],
            )
        )
    return tuple(specs)


REGISTRY: tuple[ToolSpec, ...] = _build()
BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in REGISTRY}


def export_specs() -> list[dict[str, Any]]:
    """Tool descriptors in catalog order. The list does not include handlers."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "mutates_state": spec.mutates_state,
            "input_schema": spec.input_schema,
            "output_schema": spec.output_schema,
        }
        for spec in REGISTRY
    ]
