"""Strategy DSL parser and formatter.

`parse_strategy` reads a strategy file into canonical IR v1.
`format_strategy` prints canonical IR back to the DSL. Formatting the parse
of that text is stable.
"""

from app.dsl.errors import StrategyError
from app.dsl.format import format_strategy
from app.dsl.parser import parse_strategy

__all__ = ["StrategyError", "format_strategy", "parse_strategy"]
