class MeerkatError(Exception):
    """Base exception for Meerkat SDK errors."""

    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


class RateLimitError(MeerkatError):
    """Raised when API rate limit is exceeded."""
    pass


class AuthError(MeerkatError):
    """Raised when API key is invalid or missing."""
    pass
