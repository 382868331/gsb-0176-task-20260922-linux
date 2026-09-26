"""regmirror -- atomic scanning of versioned industrial register mirrors.

Offline, standard-library-only module.  Public surface:

* :class:`FieldSpec` / :class:`FieldValue` -- field layout and values
* :class:`RegisterGroup` / :class:`ScanConfig` -- scan configuration
* :class:`Scanner` / :class:`Snapshot` / :class:`GroupSnapshot` -- scanning
* :class:`DeviceMirror` / :class:`Change` -- deterministic scripted image
* error hierarchy rooted at :class:`RegMirrorError`
"""

from .bitfields import (
    BitFieldSpec,
    BitLayout,
    LayoutSnapshot,
    LayoutValue,
    decode_bitfields,
)
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

__version__ = "1.1.0"

__all__ = [
    "AddressError",
    "BitFieldSpec",
    "BitLayout",
    "Change",
    "ConfigError",
    "DeviceMirror",
    "DeviceReadError",
    "FieldSpec",
    "FieldValue",
    "GroupSnapshot",
    "InconsistentRead",
    "InconsistentScanError",
    "LayoutSnapshot",
    "LayoutValue",
    "MAX_COMMITS_PER_SCAN",
    "RegisterGroup",
    "RegMirrorError",
    "ScanConfig",
    "Scanner",
    "Snapshot",
    "decode_bitfields",
    "decode_field",
    "encode_field",
    "__version__",
]
