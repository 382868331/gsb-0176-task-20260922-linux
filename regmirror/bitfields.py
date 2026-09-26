"""Bit-level field tables decoded from one verified register mirror.

This module adds a second, complementary decoding entry point on top of
the atomic scanner: the caller supplies a *field table* describing, for
each field, a start bit, a width, signedness and (at table level) the
word order of the 16-bit registers.  Fields may span any number of
registers -- including unaligned fields crossing three or more words.

Bitstring model (fixed public contract):

* A layout covers n_words consecutive 16-bit registers starting at
  base_address; n_words is derived from the highest bit used.
* The registers are assembled into one logical bitstring according to
  the layout's word_order:

  - "little" -- the lowest-address register is the least
    significant word;
  - "big"    -- the lowest-address register is the most
    significant word.

* Inside the logical bitstring, bit 0 is the least significant bit of
  the least significant word.  A field with start_bit=s, width=w
  occupies logical bits [s, s+w); its unsigned raw value is
  (logical >> s) & ((1 << w) - 1), interpreted as two's complement
  when signed is true.

Unions and overlap:

* Fields may explicitly declare themselves as different views of one
  union by sharing a non-empty union tag; fields inside the same
  union may overlap freely.
* Any other bit overlap is an illegal layout and is rejected with
  ConfigError *before* any device read happens.

Atomicity contract:

* Scanner.scan_layout decodes exclusively from the single
  version-gated mirror it just committed -- it never re-reads the
  device for an individual field.  A missing word, a generation
  (version) conflict or an illegal layout raises and yields *no*
  partial decode results.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .errors import ConfigError

__all__ = [
    "BitFieldSpec",
    "BitLayout",
    "LayoutValue",
    "LayoutSnapshot",
    "decode_bitfields",
]

ORDERS = ("big", "little")


def _check_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{name} must be an int, got {type(value).__name__}"
        )
    return value


@dataclass(frozen=True)
class BitFieldSpec:
    """One bit-level field inside a BitLayout.

    Attributes:
        name:      unique field name within the layout.
        start_bit: offset (in bits, 0 = LSB of the logical bitstring).
        width:     field width in bits (>= 1); may span any number of
                   16-bit registers.
        signed:    interpret the raw bits as two's complement.
        union:     optional tag; fields sharing a non-None tag are
                   declared views of the same union and may overlap.
    """

    name: str
    start_bit: int
    width: int
    signed: bool = False
    union: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ConfigError(
                f"field name must be a non-empty str, got {self.name!r}"
            )
        _check_int(f"field {self.name!r}: start_bit", self.start_bit)
        if self.start_bit < 0:
            raise ConfigError(
                f"field {self.name!r}: start_bit must be >= 0, "
                f"got {self.start_bit}"
            )
        _check_int(f"field {self.name!r}: width", self.width)
        if self.width < 1:
            raise ConfigError(
                f"field {self.name!r}: width must be >= 1, got {self.width}"
            )
        if not isinstance(self.signed, bool):
            raise ConfigError(
                f"field {self.name!r}: signed must be a bool, "
                f"got {type(self.signed).__name__}"
            )
        if self.union is not None and (
            not isinstance(self.union, str) or not self.union
        ):
            raise ConfigError(
                f"field {self.name!r}: union must be None or a non-empty "
                f"str, got {self.union!r}"
            )

    @property
    def end_bit(self) -> int:
        """Exclusive end of the field in the logical bitstring."""
        return self.start_bit + self.width


@dataclass(frozen=True)
class LayoutValue:
    """One decoded bit field plus the registers it was actually read from.

    registers is a tuple of (address, mask) pairs sorted by address;
    mask marks the bits of that 16-bit register the field occupies.
    This is exactly the register/mask footprint of the value inside the
    committed mirror.
    """

    name: str
    value: int
    registers: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class LayoutSnapshot:
    """All fields of one layout, decoded from a single committed mirror."""

    name: str
    version: int  # even version word the mirror was committed under
    attempts: int  # attempts the version gate needed (1 = clean)
    values: tuple[LayoutValue, ...]

    def __getitem__(self, field_name: str) -> LayoutValue:
        for v in self.values:
            if v.name == field_name:
                return v
        raise KeyError(field_name)


class BitLayout:
    """A bit-level field table over consecutive 16-bit registers.

    The layout is fully validated at construction -- illegal layouts
    (overlapping non-union fields, duplicate names, out-of-range
    addresses) raise ConfigError before any device read.
    """

    def __init__(
        self,
        name: str,
        version_address: int,
        base_address: int,
        fields: Sequence[BitFieldSpec],
        word_order: str = "little",
    ):
        if not isinstance(name, str) or not name:
            raise ConfigError(
                f"layout name must be a non-empty str, got {name!r}"
            )
        _check_int("version_address", version_address)
        if not 0 <= version_address <= 0xFFFF:
            raise ConfigError(
                f"layout {name!r}: version_address {version_address} "
                "outside 0..65535"
            )
        _check_int("base_address", base_address)
        if not 0 <= base_address <= 0xFFFF:
            raise ConfigError(
                f"layout {name!r}: base_address {base_address} "
                "outside 0..65535"
            )
        if word_order not in ORDERS:
            raise ConfigError(
                f"layout {name!r}: word_order must be 'big' or 'little', "
                f"got {word_order!r}"
            )
        fields = tuple(fields)
        if not fields:
            raise ConfigError(f"layout {name!r}: at least one field required")
        for f in fields:
            if not isinstance(f, BitFieldSpec):
                raise ConfigError(
                    f"layout {name!r}: fields must be BitFieldSpec, "
                    f"got {type(f).__name__}"
                )
        names = [f.name for f in fields]
        if len(set(names)) != len(names):
            raise ConfigError(
                f"layout {name!r}: duplicate field names {sorted(names)}"
            )
        # Overlap is legal only between views explicitly declared as the
        # same union; anything else is an ambiguous layout and rejected
        # up front.
        ordered = sorted(fields, key=lambda f: (f.start_bit, f.end_bit))
        for a, b in zip(ordered, ordered[1:]):
            if b.start_bit < a.end_bit:
                same_union = a.union is not None and a.union == b.union
                if not same_union:
                    raise ConfigError(
                        f"layout {name!r}: fields {a.name!r} and "
                        f"{b.name!r} overlap at bit {b.start_bit} and are "
                        "not declared as views of one union"
                    )
        n_words = -(-max(f.end_bit for f in fields) // 16)  # ceil
        last_address = base_address + n_words - 1
        if last_address > 0xFFFF:
            raise ConfigError(
                f"layout {name!r}: registers {base_address}..{last_address} "
                "outside 0..65535"
            )
        if base_address <= version_address <= last_address:
            raise ConfigError(
                f"layout {name!r}: version register {version_address} "
                "overlaps the data registers "
                f"{base_address}..{last_address}"
            )
        self.name = name
        self.version_address = version_address
        self.base_address = base_address
        self.fields = fields
        self.word_order = word_order
        self.n_words = n_words

    @property
    def last_address(self) -> int:
        return self.base_address + self.n_words - 1

    def register_of(self, bit: int) -> tuple[int, int]:
        """Map a logical bit to (register_address, bit_in_register)."""
        word_index, bit_in_word = divmod(bit, 16)
        if self.word_order == "little":
            address = self.base_address + word_index
        else:
            address = self.base_address + (self.n_words - 1 - word_index)
        return address, bit_in_word


def decode_bitfields(
    layout: BitLayout, words: Sequence[int]
) -> tuple[LayoutValue, ...]:
    """Decode every field of a layout from one register mirror.

    words must be exactly the layout.n_words 16-bit registers starting
    at layout.base_address -- typically the committed words of a single
    version-gated scan.  A wrong word count or a non-16-bit word raises
    ConfigError; the function is all-or-nothing and never returns a
    partial decode.
    """
    if not isinstance(layout, BitLayout):
        raise ConfigError(
            f"layout must be a BitLayout, got {type(layout).__name__}"
        )
    if isinstance(words, (str, bytes)) or not isinstance(words, Sequence):
        raise ConfigError(
            f"words must be a sequence of {layout.n_words} 16-bit words, "
            f"got {type(words).__name__}"
        )
    words = tuple(words)
    if len(words) != layout.n_words:
        raise ConfigError(
            f"layout {layout.name!r}: need {layout.n_words} words, "
            f"got {len(words)}"
        )
    for i, w in enumerate(words):
        if isinstance(w, bool) or not isinstance(w, int):
            raise ConfigError(
                f"layout {layout.name!r}: word {i} is "
                f"{type(w).__name__}, expected int"
            )
        if not 0 <= w <= 0xFFFF:
            raise ConfigError(
                f"layout {layout.name!r}: word {i} = {w}, not a 16-bit word"
            )
    # Assemble the logical bitstring per the layout's word order.
    ordered = words if layout.word_order == "little" else words[::-1]
    logical = 0
    for word_index, word in enumerate(ordered):
        logical |= word << (16 * word_index)
    values = []
    for f in layout.fields:
        raw = (logical >> f.start_bit) & ((1 << f.width) - 1)
        if f.signed and raw & (1 << (f.width - 1)):
            raw -= 1 << f.width
        masks: dict[int, int] = {}
        for bit in range(f.start_bit, f.end_bit):
            address, bit_in_word = layout.register_of(bit)
            masks[address] = masks.get(address, 0) | (1 << bit_in_word)
        registers = tuple(sorted(masks.items()))
        values.append(LayoutValue(f.name, raw, registers))
    return tuple(values)
