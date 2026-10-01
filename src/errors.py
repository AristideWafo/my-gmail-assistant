class ConfigurationError(RuntimeError):
    """A misconfiguration whose message is safe to show to the user: it must never embed a secret."""


class SchemaVersionError(RuntimeError):
    """The database was written by a newer version of the app than the one opening it."""


class BackupError(RuntimeError):
    """The store produced a copy that cannot be trusted for a restore."""
