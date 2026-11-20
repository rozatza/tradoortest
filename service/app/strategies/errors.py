"""Failures the strategy API turns into a JSON ``{error}`` envelope.

Messages on these types are fixed. Callers must not attach a filesystem
path, a SQL statement, or ``str(sqlite_error)``.
"""


class StoreUnavailable(Exception):
    """The strategy database cannot be opened or used.

    Startup leaves ``app.state.strategies`` unset when this is raised from
    ``open_store``, so a later request can try again.
    """

    def __init__(self) -> None:
        super().__init__("strategy storage is unavailable")


class StrategyNotFound(Exception):
    """No active strategy for this owner and id.

    Archived strategies use the same error. The API does not say which.
    """


class VersionNotFound(Exception):
    """The strategy is active and the version number is not stored."""


class Rejected(Exception):
    """The body is not a valid strategy. Nothing was written.

    ``diagnostics`` is a short list of ``{code, message, path}`` objects,
    plus ``line`` and ``column`` when the parser has them.
    """

    def __init__(
        self,
        code: str,
        message: str,
        path: str | None,
        diagnostics: list[dict[str, object]] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        self.diagnostics = diagnostics
        super().__init__(code)
