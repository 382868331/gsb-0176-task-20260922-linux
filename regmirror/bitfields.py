"""Bit-level field tables decoded from one verified register mirror.

This module adds a second, complementary decoding entry point on top of
the atomic scanner: the caller describes a *field table* -- each field
has a start bit, a width, signedness and a word order over the 16-bit
registers -- and decodes every field from **one** already verified
mirror (a single committed scan).  Fields may span any number of
registers.  Besides the value, the decoder reports exactly which
registers each value used and the 16-bit mask applied in each of them.

Bit model (fixed public contract):

* Register bit 0 is the LSB of a 16-bit register, bit 15 its MSB.
* A field occupies the logical bit range
  [bit_offset, bit_offset + width); logical word w (bits
  16*w..16*w+15) always lives in register base_address + w.
* word_order="little": the lowest-address register of the field's
  span holds the field's least significant bits.
* word_order="big": the lowest-address register of the field's
  span holds the field's most significant bits.
* The unsigned bit string is the field's bits; for signed=True fields
  the value is the two's-complement interpretation over width bits.

Layout safety: two fields whose register masks intersect are rejected
with ConfigError **before any scan**, unless both explicitly declare
the same non-empty union tag (different views of the same underlying
bits, like a C union).

Atomicity: decode_bitfields validates that *every* register needed by
*every* field is present and well-formed before decoding anything, so
missing words or an illegal layout never yield partial results.
Generation conflicts are handled one layer below by the scanner's
version gate (InconsistentScanError), which likewise produces no
values at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .errors import ConfigError, DeviceReadError, RegMirrorError

__all__ = [
    "BitFieldSpec",
    "BitFieldTable",
    "BitFieldGroup",
    "BitFieldValue",
    "IncompleteImageError",
    "decode_bitfields",
    "register_masks",
]

MAX_WIDTH = 64
MAX_BIT_OFFSET = 0xFFFF * 16  # one full 16-bit address space of bits


class IncompleteImageError(RegMirrorError):
    """The mirror handed to the decoder is missing required registers.

    Raised before any value is decoded: the result is all or nothing.
    """


def _check_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{name} must be an int, got {type(value).__name__}"
        )
    return value


@dataclass(frozen=True)
class BitFieldSpec:
    """One bit-level field over the 16-bit registers of a table.

    Attributes:
        name:       unique field name inside its table.
        bit_offset: start bit (LSB side) in the field's logical bit
                    string; 0 <= bit_offset.
        width:      field width in bits, 1..64.
        signed:     interpret the bits as two's complement.
        word_order: "big" or "little" (see module docstring).
        union:      None or a non-empty tag; fields sharing a tag
                    may overlap (they are views of the same bits).
    """

    name: str
    bit_offset: int
    width: int
    signed: bool = False
    word_order: str = "big"
    union: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ConfigError(
                f"field name must be a non-empty str, got {self.name!r}"
            )
        bit_offset = _check_int(
            f"field {self.name!r}: bit_offset", self.bit_offset
        )
        if not 0 <= bit_offset <= MAX_BIT_OFFSET:
            raise ConfigError(
                f"field {self.name!r}: bit_offset {bit_offset} outside "
                f"0..{MAX_BIT_OFFSET}"
            )
        width = _check_int(f"field {self.name!r}: width", self.width)
        if not 1 <= width <= MAX_WIDTH:
            raise ConfigError(
                f"field {self.name!r}: width must be in 1..{MAX_WIDTH}, "
                f"got {width}"
            )
        if not isinstance(self.signed, bool):
            raise ConfigError(
                f"field {self.name!r}: signed must be a bool, "
                f"got {type(self.signed).__name__}"
            )
        if self.word_order not in ("big", "little"):
            raise ConfigError(
                f"field {self.name!r}: word_order must be 'big' or "
                f"'little', got {self.word_order!r}"
            )
        if self.union is not None and (
            not isinstance(self.union, str) or not self.union
        ):
            raise ConfigError(
                f"field {self.name!r}: union must be None or a "
                f"non-empty str, got {self.union!r}"
            )

    @property
    def word_span(self) -> tuple[int, int]:
        """Inclusive [lo_word, hi_word] logical word indices touched."""
        lo = self.bit_offset // 16
        hi = (self.bit_offset + self.width - 1) // 16
        return (lo, hi)


def register_masks(
    spec: BitFieldSpec, base_address: int
) -> tuple[tuple[int, int], ...]:
    """Return ((register_address, mask), ...) for one field.

    mask is the 16-bit mask of the bits the field actually uses in
    that register.  The result is sorted by register address.
    """
    lo, hi = spec.word_span
    shift = spec.bit_offset - 16 * lo  # field's LSB position in assembly
    out = []
    for w in range(lo, hi + 1):
        # Register base+w always holds logical word w; k is the word's
        # position in the field's own assembly (order-dependent).
        k = (w - lo) if spec.word_order == "little" else (hi - w)
        first = max(shift, 16 * k)
        last = min(shift + spec.width, 16 * k + 16)
        mask = ((1 << (last - first)) - 1) << (first - 16 * k)
        out.append((base_address + w, mask))
    out.sort()
    return tuple(out)


class BitFieldTable:
    """A validated set of bit fields over one register region.

    All layout validation happens here, before any device read: illegal
    or ambiguous layouts are rejected with ConfigError and can never
    reach the decoder.
    """

    def __init__(
        self,
        base_address: int,
        fields: Sequence[BitFieldSpec],
    ):
        base_address = _check_int("base_address", base_address)
        if not 0 <= base_address <= 0xFFFF:
            raise ConfigError(
                f"base_address {base_address} outside 0..65535"
            )
        fields = tuple(fields)
        if not fields:
            raise ConfigError("at least one field is required")
        for f in fields:
            if not isinstance(f, BitFieldSpec):
                raise ConfigError(
                    f"fields must be BitFieldSpec, got {type(f).__name__}"
                )
        names = [f.name for f in fields]
        if len(set(names)) != len(names):
            raise ConfigError(f"duplicate field names {sorted(names)}")
        # Overlap rule: intersecting register masks are illegal unless
        # both fields declare the same union tag (views of one union).
        masks = {
            f.name: dict(register_masks(f, base_address)) for f in fields
        }
        ordered = sorted(fields, key=lambda f: (f.bit_offset, f.name))
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                shared = set(masks[a.name]) & set(masks[b.name])
                clash = next(
                    (
                        (addr, masks[a.name][addr] & masks[b.name][addr])
                        for addr in sorted(shared)
                        if masks[a.name][addr] & masks[b.name][addr]
                    ),
                    None,
                )
                if clash is None:
                    continue
                addr, bits = clash
                if a.union is None or a.union != b.union:
                    raise ConfigError(
                        f"fields {a.name!r} and {b.name!r} overlap at "
                        f"register {addr} (mask 0x{bits:04X}); declare "
                        "the same union tag for intentional views"
                    )
        last = max(
            addr
            for f in fields
            for addr, _ in register_masks(f, base_address)
        )
        if last > 0xFFFF:
            raise ConfigError(
                f"table extends to register {last}, outside 0..65535"
            )
        self.base_address = base_address
        self.fields = fields

    @property
    def register_span(self) -> tuple[int, int]:
        """Inclusive [first, last] register addresses the table needs."""
        pairs = [
            addr
            for f in self.fields
            for addr, _ in register_masks(f, self.base_address)
        ]
        return (min(pairs), max(pairs))

    def registers_for(
        self, field_name: str
    ) -> tuple[tuple[int, int], ...]:
        """((address, mask), ...) used by one named field."""
        for f in self.fields:
            if f.name == field_name:
                return register_masks(f, self.base_address)
        raise KeyError(field_name)


@dataclass(frozen=True)
class BitFieldValue:
    """One decoded bit field: value plus the exact registers/masks used."""

    name: str
    raw: int  # signed-adjusted integer value (two's complement applied)
    unsigned: int  # the raw bit string as an unsigned integer
    registers: tuple[int, ...]  # register addresses actually used
    masks: tuple[int, ...]  # 16-bit mask per register, aligned


def _decode_one(
    spec: BitFieldSpec, base: int, words: Mapping[int, int]
) -> BitFieldValue:
    lo, hi = spec.word_span
    logical = 0
    for w in range(lo, hi + 1):
        # Logical word w always lives in register base + w; word_order
        # only chooses which register of the span is most significant.
        if spec.word_order == "big":
            shift = 16 * (hi - w)
        else:
            shift = 16 * (w - lo)
        logical |= words[base + w] << shift
    unsigned = (logical >> (spec.bit_offset - 16 * lo)) & (
        (1 << spec.width) - 1
    )
    raw = unsigned
    if spec.signed and unsigned & (1 << (spec.width - 1)):
        raw = unsigned - (1 << spec.width)
    pairs = register_masks(spec, base)
    return BitFieldValue(
        spec.name,
        raw,
        unsigned,
        tuple(a for a, _ in pairs),
        tuple(m for _, m in pairs),
    )


def decode_bitfields(
    table: BitFieldTable, words: Mapping[int, int]
) -> tuple[BitFieldValue, ...]:
    """Decode every field of table from one verified mirror.

    words maps absolute register addresses to 16-bit words, exactly
    as committed by a single scan.  Every register needed by any field
    must be present and well-formed; otherwise IncompleteImageError /
    DeviceReadError is raised **before any field is decoded** -- no
    partial results.
    """
    if not isinstance(table, BitFieldTable):
        raise ConfigError(
            f"table must be a BitFieldTable, got {type(table).__name__}"
        )
    if isinstance(words, (str, bytes)) or not isinstance(words, Mapping):
        raise ConfigError(
            f"words must be a mapping of address -> word, "
            f"got {type(words).__name__}"
        )
    needed = sorted(
        {
            addr
            for f in table.fields
            for addr, _ in register_masks(f, table.base_address)
        }
    )
    missing = [a for a in needed if a not in words]
    if missing:
        raise IncompleteImageError(
            f"mirror is missing register(s) {missing}; refusing to "
            "decode partial results"
        )
    for addr in needed:
        w = words[addr]
        if (
            isinstance(w, bool)
            or not isinstance(w, int)
            or not 0 <= w <= 0xFFFF
        ):
            raise DeviceReadError(
                f"mirror word at register {addr} is {w!r}, "
                "expected a 16-bit int"
            )
    return tuple(
        _decode_one(f, table.base_address, words) for f in table.fields
    )


class BitFieldGroup:
    """A bit-field table guarded by one version register.

    Plugs into regmirror.Scanner exactly like a RegisterGroup: the
    scanner reads the table's register span **once** inside the
    version gate and this group's decode() turns that single verified
    mirror into values.
    """

    def __init__(
        self, name: str, version_address: int, table: BitFieldTable
    ):
        if not isinstance(name, str) or not name:
            raise ConfigError(
                f"group name must be a non-empty str, got {name!r}"
            )
        version_address = _check_int("version_address", version_address)
        if not 0 <= version_address <= 0xFFFF:
            raise ConfigError(
                f"group {name!r}: version_address {version_address} "
                "outside 0..65535"
            )
        if not isinstance(table, BitFieldTable):
            raise ConfigError(
                f"group {name!r}: table must be a BitFieldTable, "
                f"got {type(table).__name__}"
            )
        first, last = table.register_span
        if first <= version_address <= last:
            raise ConfigError(
                f"group {name!r}: version register {version_address} "
                f"overlaps table registers {first}..{last}"
            )
        self.name = name
        self.version_address = version_address
        self.table = table

    @property
    def data_span(self) -> tuple[int, int]:
        return self.table.register_span

    def decode(
        self, data: Mapping[int, int]
    ) -> tuple[BitFieldValue, ...]:
        return decode_bitfields(self.table, data)
