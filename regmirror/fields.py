"""Field specifications and 16-bit-register word/byte-order codec.

A field is a signed/unsigned 32- or 64-bit integer laid out over 16-bit
registers, with an explicit word order (order of the 16-bit words) and
byte order (order of the two bytes inside each word), plus an exact
``fractions.Fraction`` scale applied to the raw integer.

Endianness model (fixed public contract):

* ``byte_order="big"``    -- inside one register, the high byte is first.
* ``byte_order="little"`` -- inside one register, the low byte is first.
* ``word_order="big"``    -- the lowest-address register is the most
                             significant word.
* ``word_order="little"`` -- the lowest-address register is the least
                             significant word.

The decoded logical value is ``raw * scale`` as an exact ``Fraction``;
``FieldValue.value_float`` is the correctly rounded double of that
rational (see README for the error statement).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .errors import ConfigError

__all__ = ["FieldSpec", "FieldValue", "encode_field", "decode_field"]

WIDTHS = (32, 64)
ORDERS = ("big", "little")


def _check_order(name: str, value: object) -> str:
    if value not in ORDERS:
        raise ConfigError(
            f"{name} must be 'big' or 'little', got {value!r}"
        )
    return value  # type: ignore[return-value]


def _coerce_scale(value: object) -> Fraction:
    """Coerce a scale to an exact Fraction, rejecting NaN/Infinity.

    ``int`` and ``Fraction`` are exact.  A finite ``float`` is converted
    exactly (its binary value, no silent rounding); NaN and the
    infinities are rejected outright.  ``bool`` is rejected everywhere.
    """
    if isinstance(value, bool):
        raise ConfigError("scale must not be a bool")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ConfigError(f"scale must be finite, got {value!r}")
        return Fraction(value)
    raise ConfigError(
        f"scale must be an int, float or Fraction, got {type(value).__name__}"
    )


@dataclass(frozen=True)
class FieldSpec:
    """Layout and scaling of one logical field over 16-bit registers."""

    name: str
    address: int
    width: int = 32
    signed: bool = False
    word_order: str = "big"
    byte_order: str = "big"
    scale: Fraction = Fraction(1)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ConfigError(
                f"field name must be a non-empty str, got {self.name!r}"
            )
        if isinstance(self.address, bool) or not isinstance(self.address, int):
            raise ConfigError(
                f"field {self.name!r}: address must be an int, "
                f"got {type(self.address).__name__}"
            )
        if not 0 <= self.address <= 0xFFFF:
            raise ConfigError(
                f"field {self.name!r}: address {self.address} outside 0..65535"
            )
        if self.width not in WIDTHS:
            raise ConfigError(
                f"field {self.name!r}: width must be 32 or 64, got {self.width}"
            )
        if not isinstance(self.signed, bool):
            raise ConfigError(
                f"field {self.name!r}: signed must be a bool, "
                f"got {type(self.signed).__name__}"
            )
        _check_order(f"field {self.name!r}: word_order", self.word_order)
        _check_order(f"field {self.name!r}: byte_order", self.byte_order)
        object.__setattr__(self, "scale", _coerce_scale(self.scale))

    @property
    def n_words(self) -> int:
        return self.width // 16

    @property
    def last_address(self) -> int:
        return self.address + self.n_words - 1

    @property
    def raw_range(self) -> tuple[int, int]:
        """Inclusive [min, max] of the raw integer for this field."""
        bits = self.width
        if self.signed:
            return (-(1 << (bits - 1)), (1 << (bits - 1)) - 1)
        return (0, (1 << bits) - 1)


@dataclass(frozen=True)
class FieldValue:
    """One decoded field value inside a committed snapshot."""

    name: str
    raw: int
    value: Fraction  # exact: raw * scale

    @property
    def value_float(self) -> float:
        """Correctly rounded double of the exact value (<= 0.5 ulp error)."""
        return float(self.value)


def _word_to_bytes(word: int, byte_order: str) -> tuple[int, int]:
    hi, lo = word >> 8, word & 0xFF
    return (hi, lo) if byte_order == "big" else (lo, hi)


def decode_raw(spec: FieldSpec, words: tuple[int, ...]) -> int:
    """Decode the raw integer from register words per the field's orders."""
    if len(words) != spec.n_words:
        raise ConfigError(
            f"field {spec.name!r}: need {spec.n_words} words, got {len(words)}"
        )
    chunks = [_word_to_bytes(w, spec.byte_order) for w in words]
    if spec.word_order == "little":
        chunks.reverse()  # most significant word first
    data = bytes(b for chunk in chunks for b in chunk)
    raw = int.from_bytes(data, "big", signed=False)
    if spec.signed:
        sign_bit = 1 << (spec.width - 1)
        if raw & sign_bit:
            raw -= 1 << spec.width
    return raw


def decode_field(spec: FieldSpec, words: tuple[int, ...]) -> FieldValue:
    """Decode register words into a :class:`FieldValue` (exact scale)."""
    raw = decode_raw(spec, words)
    return FieldValue(spec.name, raw, Fraction(raw) * spec.scale)


def encode_field(spec: FieldSpec, value: int) -> tuple[int, ...]:
    """Encode a raw integer into register words (inverse of decode_raw).

    Used by the scripted device image; validates the value against the
    field's raw range instead of silently truncating.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"field {spec.name!r}: value must be an int, "
            f"got {type(value).__name__}"
        )
    lo, hi = spec.raw_range
    if not lo <= value <= hi:
        raise ConfigError(
            f"field {spec.name!r}: value {value} outside raw range "
            f"[{lo}, {hi}]"
        )
    raw = value % (1 << spec.width)  # two's complement for negatives
    data = raw.to_bytes(spec.n_words * 2, "big", signed=False)
    chunks = [tuple(data[i : i + 2]) for i in range(0, len(data), 2)]
    if spec.word_order == "little":
        chunks.reverse()  # lowest-address word is least significant
    words = []
    for first, second in chunks:
        if spec.byte_order == "big":
            words.append((first << 8) | second)
        else:
            words.append((second << 8) | first)
    return tuple(words)
