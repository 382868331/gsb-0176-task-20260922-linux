"""regmirror -- atomic scanning of versioned industrial register mirrors.

Offline, standard-library-only module.  Public surface:

* :class:`FieldSpec` / :class:`FieldValue` -- field layout and values
* :class:`RegisterGroup` / :class:`ScanConfig` -- scan configuration
* :class:`Scanner` / :class:`Snapshot` / :class:`GroupSnapshot` -- scanning
* :class:`DeviceMirror` / :class:`Change` -- deterministic scripted image
* error hierarchy rooted at :class:`RegMirrorError`
"""

from .device import Change, DeviceMirror
from .bitfields import (
    BitFieldGroup,
    BitFieldSpec,
    BitFieldTable,
    BitFieldValue,
    IncompleteImageError,
    decode_bitfields,
    register_masks,
)
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

__version__ = "1.0.0"

__all__ = [
    "AddressError",
    "BitFieldGroup",
    "BitFieldSpec",
    "BitFieldTable",
    "BitFieldValue",
    "Change",
    "ConfigError",
    "DeviceMirror",
    "DeviceReadError",
    "FieldSpec",
    "FieldValue",
    "GroupSnapshot",
    "IncompleteImageError",
    "InconsistentRead",
    "InconsistentScanError",
    "MAX_COMMITS_PER_SCAN",
    "RegisterGroup",
    "RegMirrorError",
    "ScanConfig",
    "Scanner",
    "Snapshot",
    "decode_bitfields",
    "decode_field",
    "encode_field",
    "register_masks",
    "__version__",
]
