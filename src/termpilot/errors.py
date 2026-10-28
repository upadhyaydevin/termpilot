"""Base exception for expected, user-facing failures."""


class TermPilotError(Exception):
    """An expected failure with a message that is safe to show the user."""
