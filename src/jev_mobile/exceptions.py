class JevMobileError(Exception):
    """Base exception for jev-mobile."""


class HierarchyParseError(JevMobileError):
    """Raised when a device hierarchy cannot be parsed safely."""


class HierarchyDumpError(JevMobileError):
    """Raised when a device hierarchy cannot be captured."""


class StaleSnapshotError(JevMobileError):
    """Raised when an element reference belongs to another UI snapshot."""


class ElementNotFoundError(JevMobileError):
    """Raised when an element does not exist in the current UI snapshot."""


class DeviceActionError(JevMobileError):
    """Raised when a device rejects or cannot execute a UI action."""


class DeviceActionTimeoutError(DeviceActionError):
    """Raised when a device UI action times out."""


class ToolSessionError(JevMobileError):
    """Raised when observing outside an active tool session."""


class HandoffError(JevMobileError):
    """Raised by a user handoff handler when handoff cannot complete."""


class StaleAppCatalogError(JevMobileError):
    """Raised when an app reference belongs to an older app catalog."""


class AppNotFoundError(JevMobileError):
    """Raised when an app is not present in the current launchable catalog."""


class AgentDecisionError(JevMobileError):
    """Raised when an agent decision is malformed or outside its candidates."""
