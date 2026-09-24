"""regmirror -- atomic scanning of versioned industrial register mirrors.

Offline, standard-library-only module.  Public surface:

* :class:`FieldSpec` / :class:`FieldValue` -- field layout and values
* :class:`RegisterGroup` / :class:`ScanConfig` -- scan configuration
* :class:`Scanner` / :class:`Snapshot` / :class:`GroupSnapshot` -- scanning
* :class:`DeviceMirror` / :class:`Change` -- deterministic scripted image
* error hierarchy rooted at :class:`RegMirrorError`
"""

from .device import Change, DeviceMirror, HistoryDevice
from .errors import (
    AddressError,
    ConfigError,
    DeviceReadError,
    InconsistentRead,
    InconsistentScanError,
    RegMirrorError,
    SnapshotUnavailableError,
)
from .fields import FieldSpec, FieldValue, decode_field, encode_field
from .scanner import (
    MAX_COMMITS_PER_SCAN,
    DeviceSnapshot,
    DeviceTarget,
    GroupSnapshot,
    Mirror,
    RegisterGroup,
    ScanConfig,
    Scanner,
    Snapshot,
)

__version__ = "1.1.0"

__all__ = [
    "AddressError",
    "Change",
    "ConfigError",
    "DeviceMirror",
    "DeviceSnapshot",
    "DeviceTarget",
    "DeviceReadError",
    "FieldSpec",
    "FieldValue",
    "GroupSnapshot",
    "HistoryDevice",
    "InconsistentRead",
    "InconsistentScanError",
    "MAX_COMMITS_PER_SCAN",
    "Mirror",
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
