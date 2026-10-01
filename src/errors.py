class ConfigurationError(RuntimeError):
    """A misconfiguration whose message is safe to show to the user: it must never embed a secret."""


class BackupError(RuntimeError):
    """The store produced a copy that cannot be trusted for a restore."""
