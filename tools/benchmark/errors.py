class BenchmarkError(Exception):
    """Base class for user-facing benchmark failures."""


class ConfigurationError(BenchmarkError):
    """The benchmark configuration is invalid."""


class ValidationError(BenchmarkError):
    """A schema, manifest, or recorded artifact is invalid."""


class ExecutionError(BenchmarkError):
    """A configured process could not be executed safely."""
