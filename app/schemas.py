"""Pydantic request models and definition-spec validation."""

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from .checksum import ALGORITHMS


class Condition(BaseModel):
    field: str
    op: Literal["eq", "ne", "gt", "ge", "lt", "le", "in"]
    value: Union[int, List[int]]


class RepeatSpec(BaseModel):
    # The field is repeated `count` times, where `count` is the raw value of a
    # previously defined scalar uint field; `max` bounds that runtime count.
    field: str
    max: int = Field(ge=1, le=64)


class FieldDef(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: Literal["uint", "int", "bool", "hex", "ascii"] = "uint"
    bit_length: int = Field(gt=0, le=4096)
    endianness: Literal["big", "little"] = "big"
    scale: Optional[float] = None
    offset: Optional[float] = None
    condition: Optional[Condition] = None
    repeat: Optional[RepeatSpec] = None


class ChecksumDef(BaseModel):
    field: str
    algorithm: Literal["sum8", "xor8", "crc16-ccitt", "crc16-modbus"]


class DefinitionCreate(BaseModel):
    device_type: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    description: str = ""
    fields: List[FieldDef]
    checksum: Optional[ChecksumDef] = None


class ParseRequest(BaseModel):
    device_type: str
    version: Optional[str] = None  # omitted -> latest active version
    hex: str = Field(min_length=2)


class DetectRequest(BaseModel):
    # /detect tries every registered version itself; no version hint is allowed.
    model_config = ConfigDict(extra="forbid")

    device_type: str = Field(min_length=1, max_length=128)
    hex: str = Field(min_length=2)


class EncodeRequest(BaseModel):
    device_type: str
    version: Optional[str] = None  # omitted -> latest active version
    # values use the parse-response types: int/float for uint/int (scaled fields
    # accept floats), bool for bool, "0x.." string for hex, string for ascii.
    # The checksum field must not be provided; it is computed automatically.
    values: Dict[str, Any] = Field(default_factory=dict)


class CompatibilityRequest(BaseModel):
    old_version: str = Field(min_length=1, max_length=64)
    new_version: str = Field(min_length=1, max_length=64)


class FixtureExpectField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    # omitted -> the field must simply be present; null -> expect a field
    # without a raw value (hex/ascii).
    raw_value: Optional[Any] = None


class FixtureExpectBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    matched: Optional[bool] = None
    action: Optional[str] = None


class FixtureExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Each section is optional; supplied entries assert a subset of the
    # baseline that is captured from the referenced version at creation time.
    fields: Optional[List[FixtureExpectField]] = None
    branch_path: Optional[List[FixtureExpectBranch]] = None
    checksum_valid: Optional[bool] = None


class FixtureCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    device_type: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    hex: str = Field(min_length=2)
    # Optional assertions against the captured baseline; the full captured
    # snapshot is always what gets persisted and compared against on a run.
    expect: Optional[FixtureExpectation] = None


class FixtureRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # required to disambiguate the name (names are unique within a device
    # type, not globally)
    device_type: str = Field(min_length=1, max_length=128)
    # omitted -> re-run against the fixture's baseline version.
    target_version: Optional[str] = Field(default=None, min_length=1, max_length=64)


def validate_definition(defn: DefinitionCreate) -> None:
    """Cross-field semantic validation. Raises ValueError on problems."""
    if not defn.fields:
        raise ValueError("fields must not be empty")

    seen: set[str] = set()
    numeric_seen: set[str] = set()  # scalar fields usable in conditions
    uint_seen: set[str] = set()  # scalar uint fields usable as repeat counters
    for f in defn.fields:
        if f.name in seen:
            raise ValueError(f"duplicate field name: '{f.name}'")
        seen.add(f.name)

        if f.type == "bool" and f.bit_length != 1:
            raise ValueError(f"field '{f.name}': bool fields must be 1 bit")
        if f.type in ("hex", "ascii") and f.bit_length % 8 != 0:
            raise ValueError(
                f"field '{f.name}': {f.type} fields require bit_length to be a multiple of 8"
            )
        if f.endianness == "little":
            if f.type not in ("uint", "int"):
                raise ValueError(
                    f"field '{f.name}': little-endian only applies to uint/int fields"
                )
            if f.bit_length % 8 != 0:
                raise ValueError(
                    f"field '{f.name}': little-endian requires bit_length to be a multiple of 8"
                )
        if (f.scale is not None or f.offset is not None) and f.type not in ("uint", "int"):
            raise ValueError(
                f"field '{f.name}': scale/offset only apply to uint/int fields"
            )
        if f.condition:
            c = f.condition
            if c.field not in numeric_seen:
                raise ValueError(
                    f"field '{f.name}': condition references '{c.field}', which is not a "
                    f"previously defined scalar uint/int/bool field"
                )
            if c.op == "in" and not isinstance(c.value, list):
                raise ValueError(
                    f"field '{f.name}': operator 'in' requires a list value"
                )
            if c.op != "in" and not isinstance(c.value, int):
                raise ValueError(
                    f"field '{f.name}': operator '{c.op}' requires an integer value"
                )
        if f.repeat:
            r = f.repeat
            if r.field not in uint_seen:
                raise ValueError(
                    f"field '{f.name}': repeat counter '{r.field}' must be a previously "
                    f"defined scalar uint field"
                )
            if r.field == f.name:
                raise ValueError(
                    f"field '{f.name}': repeat counter must reference an earlier field"
                )
            # repeated fields are arrays and cannot drive later conditions/counters
        elif f.type in ("uint", "int", "bool"):
            numeric_seen.add(f.name)
            if f.type == "uint":
                uint_seen.add(f.name)

    if defn.checksum:
        target = next((f for f in defn.fields if f.name == defn.checksum.field), None)
        if target is None:
            raise ValueError(
                f"checksum field '{defn.checksum.field}' is not defined in fields"
            )
        if target.repeat is not None:
            raise ValueError(
                f"checksum field '{target.name}' must not be a repeated field"
            )
        if target.type != "uint":
            raise ValueError(f"checksum field '{target.name}' must be of type uint")
        _, width = ALGORITHMS[defn.checksum.algorithm]
        if target.bit_length != width:
            raise ValueError(
                f"checksum field '{target.name}' must be {width} bits for algorithm "
                f"'{defn.checksum.algorithm}'"
            )
