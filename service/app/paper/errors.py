"""Failures the paper deploy routes turn into a JSON ``{error}`` envelope.

Messages are fixed. They do not include a strategy document, a price, or a path.
"""


class PaperError(Exception):
    """Base for a coded paper failure."""

    status = 500
    code = "internal"
    message = "the request could not be completed"

    def __init__(self) -> None:
        super().__init__(self.code)


class PaperLimitReached(PaperError):
    """Another running version would pass ``PAPER_MAX_RUNNING``."""

    status = 409
    code = "paper_limit"
    message = "paper strategy limit reached"


class PaperNotDeployed(PaperError):
    """Undeploy found no registration for this version."""

    status = 404
    code = "not_deployed"
    message = "strategy version is not deployed to paper"


class PaperNotDeployable(PaperError):
    """The saved IR cannot run on the paper feed."""

    status = 422
    code = "not_deployable"
    message = "strategy version cannot be deployed to paper"


class PaperConflict(PaperError):
    """This version is registered with a different document."""

    status = 409
    code = "already_registered"
    message = "strategy version is already registered"
