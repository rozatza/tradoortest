"""Strategy expression tokenizer, parser, and IR converter.

Public entry points are `parse`, `parse_condition`, `parse_numeric`,
`tokenize`, `to_ir`, `from_ir`, and `format_expr`. Parsing never evaluates
the expression.
"""

from .ast import (
    Binary,
    Call,
    Expr,
    ExprType,
    IndicatorCall,
    Lit,
    Ref,
    Span,
    Unary,
    format_expr,
    to_ir,
)
from .errors import ExprError
from .lexer import MAX_NUMBER_LITERAL_DIGITS, Token, TokenKind, tokenize
from .parser import (
    MAX_EXPR_DEPTH,
    MAX_EXPR_SOURCE_LENGTH,
    from_ir,
    parse,
    parse_condition,
    parse_numeric,
)

__all__ = [
    "MAX_EXPR_DEPTH",
    "MAX_EXPR_SOURCE_LENGTH",
    "MAX_NUMBER_LITERAL_DIGITS",
    "Binary",
    "Call",
    "Expr",
    "ExprError",
    "ExprType",
    "IndicatorCall",
    "Lit",
    "Ref",
    "Span",
    "Token",
    "TokenKind",
    "Unary",
    "format_expr",
    "from_ir",
    "parse",
    "parse_condition",
    "parse_numeric",
    "to_ir",
    "tokenize",
]
