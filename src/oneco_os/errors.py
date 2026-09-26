"""Domain exceptions used across the OneCo core."""


class OneCoError(RuntimeError):
    """Base error with a user-actionable message."""


class WorkspaceError(OneCoError):
    pass


class ProjectError(OneCoError):
    pass


class SessionError(OneCoError):
    pass


class StaleSessionError(SessionError):
    pass


class AuthorizationError(OneCoError):
    pass


class ConflictError(OneCoError):
    pass
