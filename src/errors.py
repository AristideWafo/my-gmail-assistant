class ConfigurationError(RuntimeError):
    """A misconfiguration whose message is safe to show to the user: it must never embed a secret."""
