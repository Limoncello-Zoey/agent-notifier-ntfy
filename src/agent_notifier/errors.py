"""Domain exceptions with messages suitable for CLI and local API clients."""


class AgentNotifierError(Exception):
    """Base class for expected application failures."""


class ConfigError(AgentNotifierError):
    """The configuration is missing or invalid."""


class ResolutionError(AgentNotifierError):
    """A configured target cannot be resolved."""


class NotificationError(AgentNotifierError):
    """A notification request is invalid."""


class DaemonError(AgentNotifierError):
    """The local daemon cannot accept or complete a request."""
