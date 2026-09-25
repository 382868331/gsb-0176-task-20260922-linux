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
from .planner import (
    ReadBlock,
    ReadPlan,
    check_read_plan,
    normalize_intervals,
    plan_reads,
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
    "ConfigError",
    "DeviceMirror",
    "DeviceReadError",
    "FieldSpec",
    "FieldValue",
    "GroupSnapshot",
    "InconsistentRead",
    "InconsistentScanError",
    "MAX_COMMITS_PER_SCAN",
    "ReadBlock",
    "ReadPlan",
    "RegisterGroup",
    "RegMirrorError",
    "ScanConfig",
    "Scanner",
    "Snapshot",
    "check_read_plan",
    "decode_field",
    "encode_field",
    "normalize_intervals",
    "plan_reads",
    "__version__",
]
