"""Errors the engine raises."""


class UnknownComponentError(Exception):
    """The key names neither the workflow nor one of its tasks."""


class PermissionDeniedError(Exception):
    """The actor's role may not change this component by hand."""


class CascadeTooLongError(Exception):
    """A change triggered more changes than allowed; links may loop. The change was rolled back."""
