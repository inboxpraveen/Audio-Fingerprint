"""Custom exception classes."""


class FingerprintException(Exception):
    """Base exception for fingerprint system."""
    pass


class AudioProcessingError(FingerprintException):
    """Exception raised for audio processing errors."""
    pass


class StorageError(FingerprintException):
    """Exception raised for storage backend errors."""
    pass


class MatchingError(FingerprintException):
    """Exception raised for fingerprint matching errors."""
    pass


class ValidationError(FingerprintException):
    """Exception raised for input validation errors."""
    pass

