"""Industrial binary message parsing API."""

import re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Response

from . import database
from .analyzer import CompatibilityError, analyze_compatibility
from .encoder import EncodeError, encode_message
from .fixtures import capture_snapshot, compare_snapshots, verify_expectation
from .parser import ParseError, parse_message
from .schemas import (
    CompatibilityRequest,
    DefinitionCreate,
    DetectRequest,
    EncodeRequest,
    FixtureCreate,
    FixtureRunRequest,
    ParseRequest,
    validate_definition,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="Industrial Binary Message Parser",
    description=(
        "Register versioned binary message definitions (bit widths, endianness, "
        "conditional branches, checksums) and parse hex payloads against them."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Make the service usable even if the lifespan hook is bypassed (e.g. tests).
database.init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/definitions", status_code=201)
def register_definition(defn: DefinitionCreate):
    """Register a new versioned message definition."""
    try:
        validate_definition(defn)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    record = database.create_definition(
        device_type=defn.device_type,
        version=defn.version,
        description=defn.description,
        fields=[f.model_dump(exclude_none=True) for f in defn.fields],
        checksum=defn.checksum.model_dump() if defn.checksum else None,
    )
    if record is None:
        raise HTTPException(
            status_code=409,
            detail=f"definition '{defn.device_type}' version '{defn.version}' already exists",
        )
    return record


@app.get("/api/v1/definitions")
def list_definitions(
    device_type: Optional[str] = Query(default=None),
    include_inactive: bool = Query(default=False),
):
    """List registered definitions (active ones by default)."""
    return {
        "count": len(
            items := database.list_definitions(device_type, include_inactive)
        ),
        "items": items,
    }


@app.get("/api/v1/definitions/{device_type}/latest")
def get_latest_definition(device_type: str):
    """Get the latest active version for a device type."""
    record = database.get_latest(device_type, active_only=True)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"no active definition found for device type '{device_type}'",
        )
    return record


@app.get("/api/v1/definitions/{device_type}/{version}")
def get_definition(device_type: str, version: str):
    record = database.get_definition(device_type, version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{version}' not found",
        )
    return record


@app.post("/api/v1/definitions/{device_type}/{version}/deactivate")
def deactivate_definition(device_type: str, version: str):
    """Deactivate a definition. It stays parseable by explicit version but is
    excluded from listing (by default) and from latest-version lookups."""
    record = database.deactivate(device_type, version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{version}' not found",
        )
    return record


@app.post("/api/v1/definitions/{device_type}/compatibility")
def compatibility(device_type: str, req: CompatibilityRequest):
    """Analyze wire compatibility between two registered versions of the same
    device type. Conditions are partitioned into finite equivalence classes,
    reachable layouts (field bit offsets + total length) are enumerated, and
    each old-version layout must find a new-version layout with identical
    field names/types/bit widths/offsets, total length and checksum algorithm.
    Returns ``breaking`` with per-branch differences otherwise; more than 256
    equivalence-class combinations yields 422."""
    if req.old_version == req.new_version:
        raise HTTPException(
            status_code=422,
            detail="old_version and new_version must differ",
        )
    old_record = database.get_definition(device_type, req.old_version)
    if old_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{req.old_version}' not found",
        )
    new_record = database.get_definition(device_type, req.new_version)
    if new_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{device_type}' version '{req.new_version}' not found",
        )
    try:
        return analyze_compatibility(old_record, new_record)
    except CompatibilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


_HEX_CLEANER = re.compile(r"(0[xX])|[\s_-]")


def _decode_hex(hex_payload: str) -> bytes:
    """Shared by /parse and /detect: strip whitespace/separators/0x prefix and
    decode. Raises HTTP 422 on an empty or non-hex payload."""
    cleaned = _HEX_CLEANER.sub("", hex_payload)
    if not cleaned:
        raise HTTPException(status_code=422, detail="hex payload is empty")
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="invalid hex payload: non-hex characters or odd number of digits",
        )


def _select_definition(device_type: str, version: Optional[str]) -> dict:
    """Shared by /parse and /encode: an explicit version selects that definition
    (even if inactive); otherwise the latest active version is used."""
    if version:
        record = database.get_definition(device_type, version)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"definition '{device_type}' version '{version}' not found",
            )
    else:
        record = database.get_latest(device_type, active_only=True)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"no active definition found for device type '{device_type}'",
            )
    return record


@app.post("/api/v1/parse")
def parse(req: ParseRequest):
    """Parse a hex payload against a definition (latest active version unless
    an explicit version is given)."""
    record = _select_definition(req.device_type, req.version)

    data = _decode_hex(req.hex)

    try:
        result = parse_message(record["fields"], record["checksum"], data)
    except ParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "device_type": record["device_type"],
        "version": record["version"],
        "definition_active": record["active"],
        **result,
    }


@app.post("/api/v1/detect")
def detect(req: DetectRequest):
    """Identify the message version without a version hint: try every registered
    version of the device type (including inactive ones) in version order.

    A version is a candidate only when the definition fully consumes the message
    (no trailing bits) and, when a checksum is configured, its verification
    passes; definitions without a checksum are judged on full consumption alone.
    Exactly one candidate returns the parse result; zero candidates returns 422
    with the per-version failure reason; several candidates return 409 with the
    candidate list — the latest version is never picked silently.
    """
    records = database.list_definitions(req.device_type, include_inactive=True)
    if not records:
        raise HTTPException(
            status_code=404,
            detail=f"no definitions found for device type '{req.device_type}'",
        )
    records.sort(key=lambda r: database.version_key(r["version"]))

    data = _decode_hex(req.hex)

    candidates = []
    failures = []
    for record in records:
        try:
            result = parse_message(record["fields"], record["checksum"], data)
        except ParseError as exc:
            reason = str(exc)
        else:
            reason = None
            if result["trailing_bits"]:
                reason = (
                    f"definition does not fully consume the message: "
                    f"{result['trailing_bits']} trailing bit(s)"
                )
            elif record["checksum"]:
                checksum = result["checksum"]
                if checksum is None:
                    reason = (
                        f"checksum field '{record['checksum']['field']}' was not parsed "
                        f"(skipped by an earlier branch)"
                    )
                elif not checksum["valid"]:
                    reason = (
                        f"checksum mismatch: expected {checksum['expected_hex']}, "
                        f"calculated {checksum['calculated_hex']}"
                    )

        if reason is None:
            candidates.append(
                {
                    "version": record["version"],
                    "active": record["active"],
                    "result": {
                        "device_type": record["device_type"],
                        "version": record["version"],
                        "definition_active": record["active"],
                        **result,
                    },
                }
            )
        else:
            failures.append(
                {
                    "version": record["version"],
                    "active": record["active"],
                    "reason": reason,
                }
            )

    if len(candidates) == 1:
        return candidates[0]["result"]

    if not candidates:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "no registered version matches the message",
                "device_type": req.device_type,
                "failures": failures,
            },
        )

    raise HTTPException(
        status_code=409,
        detail={
            "message": "message matches multiple registered versions; "
            "an explicit version is required",
            "device_type": req.device_type,
            "candidates": [
                {"version": c["version"], "active": c["active"]} for c in candidates
            ],
        },
    )


@app.post("/api/v1/encode")
def encode(req: EncodeRequest):
    """Encode field values into a hex payload (latest active version unless an
    explicit version is given). Conditions are evaluated on raw values; the
    checksum field is computed automatically and must not be provided."""
    record = _select_definition(req.device_type, req.version)

    try:
        result = encode_message(record["fields"], record["checksum"], req.values)
    except EncodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "device_type": record["device_type"],
        "version": record["version"],
        "definition_active": record["active"],
        **result,
    }


# ------------------------------------------------------------ fixtures (回归) --


@app.post("/api/v1/fixtures", status_code=201)
def create_fixture(req: FixtureCreate):
    """Save a named parse-regression fixture.

    The fixture name is unique within a device type. The hex payload is parsed
    once against the referenced version and the resulting field snapshot
    (field names + raw_value, branch path, checksum result) is stored as the
    baseline. An optional ``expect`` block asserts fields/branches/checksum of
    that baseline at creation time; the full captured snapshot is persisted
    regardless and used by ``/run``.
    """
    record = database.get_definition(req.device_type, req.version)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{req.device_type}' version '{req.version}' not found",
        )

    data = _decode_hex(req.hex)
    try:
        result = parse_message(record["fields"], record["checksum"], data)
    except ParseError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"baseline parse failed for fixture '{req.name}': {exc}",
        )

    snapshot = capture_snapshot(result)
    if req.expect is not None:
        problems = verify_expectation(
            req.expect.model_dump(exclude_none=True), snapshot
        )
        if problems:
            raise HTTPException(
                status_code=422,
                detail={"message": "fixture expectation does not match baseline", "problems": problems},
            )

    saved = database.create_fixture(
        device_type=req.device_type,
        name=req.name,
        version=req.version,
        hex_payload=req.hex,
        snapshot=snapshot,
    )
    if saved is None:
        raise HTTPException(
            status_code=409,
            detail=f"fixture '{req.name}' already exists for device type '{req.device_type}'",
        )
    return saved


@app.get("/api/v1/fixtures")
def list_fixtures(device_type: Optional[str] = Query(default=None)):
    """List saved regression fixtures (optionally filtered by device type)."""
    items = database.list_fixtures(device_type)
    return {"count": len(items), "items": items}


@app.get("/api/v1/fixtures/{name}")
def get_fixture(name: str, device_type: Optional[str] = Query(default=None)):
    """Get a fixture by name. Names are unique only within a device type:
    pass ``?device_type=...`` to disambiguate, otherwise a name shared by
    several device types returns 409 with the candidates."""
    if device_type is not None:
        fixture = database.get_fixture(device_type, name)
        if fixture is None:
            raise HTTPException(status_code=404, detail=f"fixture '{name}' not found")
        return fixture

    matches = database.get_fixture_by_name(name)
    if not matches:
        raise HTTPException(status_code=404, detail=f"fixture '{name}' not found")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"fixture name '{name}' matches multiple device types",
                "candidates": [
                    {"device_type": m["device_type"], "version": m["version"]} for m in matches
                ],
            },
        )
    return matches[0]


@app.delete("/api/v1/fixtures/{name}", status_code=204)
def delete_fixture(name: str, device_type: Optional[str] = Query(default=None)):
    """Delete a fixture (same disambiguation rule as GET)."""
    if device_type is not None:
        if not database.delete_fixture(device_type, name):
            raise HTTPException(status_code=404, detail=f"fixture '{name}' not found")
        return Response(status_code=204)

    matches = database.get_fixture_by_name(name)
    if not matches:
        raise HTTPException(status_code=404, detail=f"fixture '{name}' not found")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"fixture name '{name}' matches multiple device types",
                "candidates": [
                    {"device_type": m["device_type"], "version": m["version"]} for m in matches
                ],
            },
        )
    database.delete_fixture(matches[0]["device_type"], name)
    return Response(status_code=204)


@app.post("/api/v1/fixtures/{name}/run")
def run_fixture(name: str, req: FixtureRunRequest):
    """Re-run a saved fixture against a parser version and compare with its
    baseline snapshot.

    The body carries the fixture's ``device_type`` (names are unique only
    within a device type) and an optional ``target_version`` (defaults to the
    fixture's baseline version). The payload is parsed against the target
    definition and compared field-by-field: field names with ``raw_value``,
    the conditional branch path, and the checksum validation result. Version
    changes surface as ``added``/``removed``/``changed`` sets plus a pass
    rate; a target version that fails to parse is reported per item (HTTP 200
    with ``parse_error``), never as a 5xx crash.
    """
    fixture = database.get_fixture(req.device_type, name)
    if fixture is None:
        raise HTTPException(
            status_code=404,
            detail=f"fixture '{name}' not found for device type '{req.device_type}'",
        )

    target_version = req.target_version or fixture["version"]
    target = database.get_definition(fixture["device_type"], target_version)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"definition '{fixture['device_type']}' version '{target_version}' not found",
        )

    data = _decode_hex(fixture["hex"])

    actual_snapshot = None
    parse_error: Optional[str] = None
    target_checksum_cfg = target["checksum"]
    try:
        result = parse_message(target["fields"], target_checksum_cfg, data)
    except ParseError as exc:
        # Keep the regression report: every baseline item becomes a failure.
        parse_error = str(exc)
        actual_snapshot = {
            "fields": [],
            "branch_path": [],
            "checksum_valid": None,
        }
    else:
        actual_snapshot = capture_snapshot(result)

    comparison = compare_snapshots(fixture["snapshot"], actual_snapshot)
    if parse_error is not None:
        comparison["errors"].insert(0, f"target parse failed: {parse_error}")
        # A failed parse cannot validate even a checksum-free definition.
        if comparison["checksum"]["passed"]:
            comparison["checksum"]["passed"] = False
            comparison["passed_checks"] -= 1
            comparison["pass_rate"] = round(
                comparison["passed_checks"] / comparison["total_checks"], 4
            )

    return {
        "name": name,
        "device_type": fixture["device_type"],
        "baseline_version": fixture["version"],
        "target_version": target_version,
        "version_changed": target_version != fixture["version"],
        "hex": fixture["hex"],
        "baseline": fixture["snapshot"],
        "actual": actual_snapshot,
        "comparison": comparison,
        "parse_error": parse_error,
        "passed": parse_error is None and comparison["passed"],
    }
