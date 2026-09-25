"""regmirror -- atomic scanning of versioned industrial register mirrors.

Offline, standard-library-only module.  Public surface:

* :class:`FieldSpec` / :class:`FieldValue` -- field layout and values
* :class:`RegisterGroup` / :class:`ScanConfig` -- scan configuration
* :class:`Scanner` / :class:`Snapshot` / :class:`GroupSnapshot` -- scanning
* :class:`ReadBlock` / :class:`GroupPlan` / :class:`ScanPlan` -- the
  inspectable read plan behind every scan (``Scanner.build_plan``)
* :func:`plan_group` / :func:`plan_scan` -- planning without a device
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
    GroupPlan,
    ReadBlock,
    ScanPlan,
    normalize_forbidden_ranges,
    plan_group,
    plan_scan,
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
    "GroupPlan",
    "GroupSnapshot",
    "InconsistentRead",
    "InconsistentScanError",
    "MAX_COMMITS_PER_SCAN",
    "ReadBlock",
    "RegisterGroup",
    "RegMirrorError",
    "ScanConfig",
    "ScanPlan",
    "Scanner",
    "Snapshot",
    "decode_field",
    "encode_field",
    "normalize_forbidden_ranges",
    "plan_group",
    "plan_scan",
    "__version__",
]
