"""Tokenizer for the strategy expression mini-language.

Keywords `AND`, `OR`, `NOT`, `true`, and `false` are recognized
case-insensitively. Numbers with an exponent are floats. A `#` starts
a comment that runs to the end of the line.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .errors import ExprError


class TokenKind(Enum):
    NUMBER = "NUMBER"
    IDENT = "IDENT"
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
    TRUE = "TRUE"
    FALSE = "FALSE"
    PLUS = "PLUS"
    MINUS = "MINUS"
    STAR = "STAR"
    SLASH = "SLASH"
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"
    EQ = "EQ"
    NEQ = "NEQ"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    COMMA = "COMMA"
    DOT = "DOT"
    ASSIGN = "ASSIGN"
    EOF = "EOF"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    lexeme: str
    offset: int
    end: int
    line: int
    column: int
    value: int | float | None = None


_KEYWORDS = {
    "and": TokenKind.AND,
    "or": TokenKind.OR,
    "not": TokenKind.NOT,
    "true": TokenKind.TRUE,
    "false": TokenKind.FALSE,
}


class Lexer:
    def __init__(self, source: str) -> None:
        self.source = source
        self.length = len(source)
        self.index = 0
        self.line = 1
        self.column = 1

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while True:
            token = self._next()
            tokens.append(token)
            if token.kind is TokenKind.EOF:
                return tokens

    def _next(self) -> Token:
        self._skip()
        offset = self.index
        line = self.line
        column = self.column
        if self.index >= self.length:
            return Token(TokenKind.EOF, "", offset, offset, line, column)

        ch = self.source[self.index]
        if _is_ident_start(ch):
            return self._ident(offset, line, column)
        if ch.isascii() and ch.isdigit():
            return self._number(offset, line, column)
        return self._symbol(offset, line, column)

    def _skip(self) -> None:
        while self.index < self.length:
            ch = self.source[self.index]
            if ch in " \t\f\v":
                self._bump()
                continue
            if ch in "\r\n":
                self._bump()
                continue
            if ch == "#":
                while self.index < self.length and self.source[self.index] not in "\r\n":
                    self._bump()
                continue
            return

    def _bump(self) -> str:
        ch = self.source[self.index]
        self.index += 1
        if ch == "\r":
            if self.index < self.length and self.source[self.index] == "\n":
                self.index += 1
            self.line += 1
            self.column = 1
        elif ch == "\n":
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        return ch

    def _ident(self, offset: int, line: int, column: int) -> Token:
        start = self.index
        self._bump()
        while self.index < self.length and _is_ident_cont(self.source[self.index]):
            self._bump()
        lexeme = self.source[start : self.index]
        kind = _KEYWORDS.get(lexeme.lower(), TokenKind.IDENT)
        return Token(kind, lexeme, offset, self.index, line, column)

    def _number(self, offset: int, line: int, column: int) -> Token:
        start = self.index
        self._digits()
        dotted = self.index < self.length and self.source[self.index] == "."
        if dotted and _digit_at(self.source, self.index + 1):
            self._bump()
            self._digits()
        if self.index < self.length and self.source[self.index] in "eE":
            self._bump()
            if self.index < self.length and self.source[self.index] in "+-":
                self._bump()
            exp_digits = self.index
            self._digits()
            if self.index == exp_digits:
                self._fail("bad number", offset, line, column)
        lexeme = self.source[start : self.index]
        if any(mark in lexeme for mark in ".eE"):
            try:
                value: int | float = float(lexeme)
            except ValueError:
                self._fail("bad number", offset, line, column)
            if not math.isfinite(value):
                self._fail("bad number", offset, line, column)
        else:
            value = int(lexeme)
        return Token(TokenKind.NUMBER, lexeme, offset, self.index, line, column, value)

    def _symbol(self, offset: int, line: int, column: int) -> Token:
        ch = self._bump()
        nxt = self.source[self.index] if self.index < self.length else ""
        kind = _TWO_CHAR.get(ch + nxt) if nxt else None
        if kind is not None:
            self._bump()
            return Token(kind, ch + nxt, offset, self.index, line, column)
        kind = _ONE_CHAR.get(ch)
        if kind is None:
            shown = ch if ch.isprintable() and ch != "'" else repr(ch)
            self._fail(f"unexpected character '{shown}'", offset, line, column)
        return Token(kind, ch, offset, self.index, line, column)

    def _digits(self) -> None:
        while self.index < self.length and _is_digit(self.source[self.index]):
            self._bump()

    def _fail(self, message: str, offset: int, line: int, column: int) -> None:
        raise ExprError(message, offset=offset, line=line, column=column)


_TWO_CHAR = {
    ">=": TokenKind.GTE,
    "<=": TokenKind.LTE,
    "==": TokenKind.EQ,
    "!=": TokenKind.NEQ,
}

_ONE_CHAR = {
    "+": TokenKind.PLUS,
    "-": TokenKind.MINUS,
    "*": TokenKind.STAR,
    "/": TokenKind.SLASH,
    ">": TokenKind.GT,
    "<": TokenKind.LT,
    "(": TokenKind.LPAREN,
    ")": TokenKind.RPAREN,
    ",": TokenKind.COMMA,
    ".": TokenKind.DOT,
    "=": TokenKind.ASSIGN,
}


def tokenize(source: str) -> list[Token]:
    """Split `source` into tokens, ending with `EOF`."""
    if not isinstance(source, str):
        raise TypeError("expression must be a string")
    return Lexer(source).tokenize()


def _is_ident_start(ch: str) -> bool:
    return ch.isascii() and (ch.isalpha() or ch == "_")


def _is_ident_cont(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch == "_")


def _is_digit(ch: str) -> bool:
    return ch.isascii() and ch.isdigit()


def _digit_at(source: str, index: int) -> bool:
    return index < len(source) and _is_digit(source[index])
