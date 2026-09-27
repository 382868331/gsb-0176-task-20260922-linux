"""regmirror -- atomic scanning of versioned industrial register mirrors.

Offline, standard-library-only module.  Public surface:

* :class:`FieldSpec` / :class:`FieldValue` -- field layout and values
* :class:`RegisterGroup` / :class:`ScanConfig` -- scan configuration
* :class:`Scanner` / :class:`Snapshot` / :class:`GroupSnapshot` -- scanning
* :class:`ScanSession` / :class:`SnapshotPublisher` -- resumable scans
  and all-or-nothing publication
* :class:`DeviceMirror` / :class:`Change` -- deterministic scripted image
* error hierarchy rooted at :class:`RegMirrorError`
"""

from .device import Change, DeviceMirror
from .errors import (
    AddressError,
    ConfigError,
    DeviceReadError,
    InconsistentRead,
    InconsistentScanError,
    RegMirrorError,
)
from .fields import FieldSpec, FieldValue, decode_field, encode_field
from .resumable import ScanSession, SnapshotPublisher, UpdateResult
from .scanner import (
    MAX_COMMITS_PER_SCAN,
    GroupSnapshot,
    RegisterGroup,
    ScanConfig,
    Scanner,
    Snapshot,
)

__version__ = "1.0.0"

__all__ = [
    "AddressError",
    "Change",
    "ConfigError",
    "DeviceMirror",
    "DeviceReadError",
    "FieldSpec",
    "FieldValue",
    "GroupSnapshot",
    "InconsistentRead",
    "InconsistentScanError",
    "MAX_COMMITS_PER_SCAN",
    "RegisterGroup",
    "RegMirrorError",
    "ScanConfig",
    "ScanSession",
    "Scanner",
    "Snapshot",
    "SnapshotPublisher",
    "UpdateResult",
    "decode_field",
    "encode_field",
    "__version__",
]
