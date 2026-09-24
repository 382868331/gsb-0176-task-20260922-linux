"""regmirror -- atomic scanning of versioned industrial register mirrors.

Offline, standard-library-only module.  Public surface:

* :class:`FieldSpec` / :class:`FieldValue` -- field layout and values
* :class:`RegisterGroup` / :class:`ScanConfig` -- scan configuration
* :class:`Scanner` / :class:`Snapshot` / :class:`GroupSnapshot` -- single
  device version-gated scanning
* :class:`DeviceMirror` / :class:`Change` -- deterministic scripted image
* :class:`CommitRecord` / :class:`CommitStore` /
  :class:`HistoricalDevice` / :class:`DeviceCommit` -- versioned commit
  histories and synthetic history-keeping devices
* :class:`MirrorRequest` / :class:`MirrorScanner` /
  :class:`MirrorSnapshot` / :class:`DeviceSnapshot` -- barrier-scoped
  atomic mirrors across several devices
* error hierarchy rooted at :class:`RegMirrorError`
"""

from .device import (
    Change,
    CommitRecord,
    CommitStore,
    DeviceCommit,
    DeviceMirror,
    HistoricalDevice,
)
from .errors import (
    AddressError,
    ConfigError,
    DeviceFailure,
    DeviceInconsistency,
    DeviceReadError,
    InconsistentRead,
    InconsistentMirrorScanError,
    InconsistentScanError,
    MissingHistory,
    MultiDeviceReadError,
    RegMirrorError,
    SnapshotUnavailableError,
)
from .fields import FieldSpec, FieldValue, decode_field, encode_field
from .mirror import (
    DeviceSnapshot,
    DeviceTarget,
    MirrorRequest,
    MirrorScanner,
    MirrorSnapshot,
)
from .scanner import (
    MAX_COMMITS_PER_SCAN,
    GroupSnapshot,
    RegisterGroup,
    ScanConfig,
    Scanner,
    Snapshot,
)

__version__ = "1.1.0"

__all__ = [
    "AddressError",
    "Change",
    "CommitRecord",
    "CommitStore",
    "ConfigError",
    "DeviceCommit",
    "DeviceFailure",
    "DeviceInconsistency",
    "DeviceMirror",
    "DeviceSnapshot",
    "DeviceTarget",
    "FieldSpec",
    "FieldValue",
    "GroupSnapshot",
    "HistoricalDevice",
    "InconsistentRead",
    "InconsistentMirrorScanError",
    "InconsistentScanError",
    "MAX_COMMITS_PER_SCAN",
    "MissingHistory",
    "MirrorRequest",
    "MirrorScanner",
    "MirrorSnapshot",
    "MultiDeviceReadError",
    "RegisterGroup",
    "RegMirrorError",
    "ScanConfig",
    "Scanner",
    "Snapshot",
    "SnapshotUnavailableError",
    "decode_field",
    "encode_field",
    "__version__",
]
