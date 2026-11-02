"""Domain errors, mapped to HTTP responses in ``app.main``."""


class StreamError(Exception):
    status_code = 400


class StreamNotFound(StreamError):
    status_code = 404


class StreamLimitReached(StreamError):
    status_code = 429


class InvalidSource(StreamError):
    status_code = 422


class ProtocolUnavailable(StreamError):
    status_code = 503
