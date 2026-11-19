"""Persistence envelopes.

Ownership lives on the stored row, not inside the canonical IR. The IR stays
stable for hashing; `user_id` says who owns the row. There is no user table.
"""

from typing import Any

from pydantic import BaseModel, Field


class StoredStrategy(BaseModel):
    """A saved strategy document owned by one user."""

    user_id: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)
    ir_version: str = Field(min_length=1)
    ir: dict[str, Any]
