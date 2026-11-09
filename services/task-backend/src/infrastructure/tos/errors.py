"""TOS error definitions."""

# Re-export TOS exceptions for convenience
try:
    import tos
    TosClientError = tos.exceptions.TosClientError
    TosServerError = tos.exceptions.TosServerError
except ImportError:
    # Define placeholder exceptions if tos is not installed
    class TosClientError(Exception):
        """TOS client error placeholder."""
        pass

    class TosServerError(Exception):
        """TOS server error placeholder."""
        pass
