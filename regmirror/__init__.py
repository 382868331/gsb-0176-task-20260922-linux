"""regmirror -- atomic scanning of versioned industrial register mirrors.

Offline, standard-library-only module.  Public surface:

* :class:`FieldSpec` / :class:`FieldValue` -- field layout and values
* :class:`RegisterGroup` / :class:`ScanConfig` -- scan configuration
* :class:`Scanner` / :class:`Snapshot` / :class:`GroupSnapshot` -- scanning
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
from .scanner import (
    MAX_COMMITS_PER_SCAN,
    GroupSnapshot,
    RegisterGroup,
    ScanConfig,
    Scanner,
    Snapshot,
)
from .writer import (
    BitWrite,
    PlannedWrite,
    WritePlan,
    commit_write_plan,
    prepare_write_plan,
)
from .errors import VersionConflictError

__version__ = "1.0.0"

__all__ = [
    "AddressError",
    "BitWrite",
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
    "PlannedWrite",
    "RegisterGroup",
    "RegMirrorError",
    "ScanConfig",
    "Scanner",
    "Snapshot",
    "VersionConflictError",
    "WritePlan",
    "commit_write_plan",
    "decode_field",
    "encode_field",
    "prepare_write_plan",
    "__version__",
]
