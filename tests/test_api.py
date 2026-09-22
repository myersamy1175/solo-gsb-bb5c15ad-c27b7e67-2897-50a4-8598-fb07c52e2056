import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="parser-test-"), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.checksum import crc16_ccitt, xor8  # noqa: E402
from app.framing import (  # noqa: E402
    MAX_BUFFER_SIZE,
    FrameSpec,
    FramingError,
    REASON_CHECKSUM,
    REASON_INVALID_LENGTH,
    REASON_NOISE,
    REASON_PARSE_ERROR,
    RejectFrame,
    StreamFramer,
)
from app.main import app  # noqa: E402

client = TestClient(app)

SENSOR_DEF = {
    "device_type": "temp_sensor",
    "version": "1.0.0",
    "description": "温湿度传感器报文",
    "fields": [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "msg_type", "type": "uint", "bit_length": 4},
        {"name": "flags", "type": "uint", "bit_length": 4},
        {
            "name": "temperature",
            "type": "int",
            "bit_length": 16,
            "endianness": "little",
            "scale": 0.1,
            "condition": {"field": "msg_type", "op": "eq", "value": 1},
        },
        {
            "name": "humidity",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "msg_type", "op": "eq", "value": 2},
        },
        {"name": "crc", "type": "uint", "bit_length": 16},
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
}


def make_message(body: bytes) -> str:
    crc = crc16_ccitt(body)
    return (body + crc.to_bytes(2, "big")).hex()


def test_register_definition():
    resp = client.post("/api/v1/definitions", json=SENSOR_DEF)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["device_type"] == "temp_sensor"
    assert data["version"] == "1.0.0"
    assert data["active"] is True
    assert len(data["fields"]) == 6


def test_register_duplicate_conflict():
    resp = client.post("/api/v1/definitions", json=SENSOR_DEF)
    assert resp.status_code == 409


def test_register_invalid_condition_reference():
    bad = {
        "device_type": "bad_dev",
        "version": "1.0.0",
        "fields": [
            {"name": "a", "type": "uint", "bit_length": 8,
             "condition": {"field": "ghost", "op": "eq", "value": 1}},
        ],
    }
    resp = client.post("/api/v1/definitions", json=bad)
    assert resp.status_code == 422


def test_register_checksum_width_mismatch():
    bad = {
        "device_type": "bad_crc",
        "version": "1.0.0",
        "fields": [{"name": "crc", "type": "uint", "bit_length": 8}],
        "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
    }
    resp = client.post("/api/v1/definitions", json=bad)
    assert resp.status_code == 422


def test_parse_temperature_branch():
    # header=0xAA, msg_type=1, flags=0, temperature raw=235 (LE) -> 23.5 °C
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "version": "1.0.0", "hex": make_message(body)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["checksum"]["valid"] is True
    assert data["checksum"]["algorithm"] == "crc16-ccitt"
    assert data["consumed_bits"] == 48

    fields = {f["name"]: f for f in data["fields"]}
    assert fields["header"]["value"] == 0xAA
    assert fields["header"]["bit_offset"] == 0
    assert fields["msg_type"]["value"] == 1
    assert fields["msg_type"]["bit_offset"] == 8
    assert fields["temperature"]["value"] == 23.5
    assert fields["temperature"]["raw_value"] == 235
    assert fields["temperature"]["bit_offset"] == 16
    assert "humidity" not in fields  # skipped by branch

    branch = {b["field"]: b for b in data["branch_path"]}
    assert branch["temperature"]["matched"] is True
    assert branch["temperature"]["action"] == "parsed"
    assert branch["humidity"]["matched"] is False
    assert branch["humidity"]["action"] == "skipped"


def test_parse_humidity_branch():
    # msg_type=2 -> humidity branch, temperature skipped
    body = bytes([0xAA, 0x20, 0x55])
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": make_message(body)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    fields = {f["name"]: f for f in data["fields"]}
    assert fields["humidity"]["value"] == 0x55
    assert fields["humidity"]["bit_offset"] == 16
    assert "temperature" not in fields
    assert data["checksum"]["valid"] is True


def test_parse_checksum_mismatch():
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    good = make_message(body)
    bad = good[:-2] + ("00" if good[-2:] != "00" else "ff")
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": bad},
    )
    assert resp.status_code == 200
    assert resp.json()["checksum"]["valid"] is False


def test_parse_truncated_message():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": "AA10"},
    )
    assert resp.status_code == 422


def test_parse_invalid_hex():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": "zzzz"},
    )
    assert resp.status_code == 422


def test_parse_unknown_device():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "nope", "hex": "AABB"},
    )
    assert resp.status_code == 404


def test_trailing_bits_warning():
    body = bytes([0xAA, 0x10, 0xEB, 0x00])
    msg = make_message(body) + "FF"  # extra byte beyond definition
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "hex": msg},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["trailing_bits"] == 8
    assert data["warnings"]


def _register(device_type, version):
    payload = {
        "device_type": device_type,
        "version": version,
        "fields": [{"name": "val", "type": "uint", "bit_length": 8}],
    }
    resp = client.post("/api/v1/definitions", json=payload)
    assert resp.status_code == 201, resp.text


def test_latest_version_and_deactivate():
    _register("meter", "1.9.0")
    _register("meter", "1.10.0")  # semver-ish: 1.10.0 > 1.9.0

    resp = client.get("/api/v1/definitions/meter/latest")
    assert resp.status_code == 200
    assert resp.json()["version"] == "1.10.0"

    resp = client.post("/api/v1/definitions/meter/1.10.0/deactivate")
    assert resp.status_code == 200
    assert resp.json()["active"] is False

    # latest now falls back to the still-active older version
    resp = client.get("/api/v1/definitions/meter/latest")
    assert resp.json()["version"] == "1.9.0"

    # explicit-version parse of an inactive definition still works
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "meter", "version": "1.10.0", "hex": "2A"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition_active"] is False
    assert resp.json()["fields"][0]["value"] == 0x2A


def test_list_definitions():
    resp = client.get("/api/v1/definitions")
    assert resp.status_code == 200
    data = resp.json()
    types = {item["device_type"] for item in data["items"]}
    assert {"temp_sensor", "meter"} <= types
    # inactive 1.10.0 hidden by default
    versions = {(i["device_type"], i["version"]) for i in data["items"]}
    assert ("meter", "1.10.0") not in versions

    resp = client.get("/api/v1/definitions", params={"include_inactive": True})
    versions = {(i["device_type"], i["version"]) for i in resp.json()["items"]}
    assert ("meter", "1.10.0") in versions

    resp = client.get("/api/v1/definitions", params={"device_type": "meter"})
    assert all(i["device_type"] == "meter" for i in resp.json()["items"])


def test_get_definition_and_404s():
    resp = client.get("/api/v1/definitions/temp_sensor/1.0.0")
    assert resp.status_code == 200
    assert resp.json()["checksum"]["algorithm"] == "crc16-ccitt"

    assert client.get("/api/v1/definitions/temp_sensor/9.9.9").status_code == 404
    assert client.get("/api/v1/definitions/ghost/latest").status_code == 404
    assert client.post("/api/v1/definitions/ghost/1.0.0/deactivate").status_code == 404


# ------------------------------------------------------------------ encode --


def test_encode_temperature_branch_roundtrip():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "version": "1.0.0",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.5},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # crc computed by the service, humidity not required on this branch
    assert data["hex"] == make_message(bytes([0xAA, 0x10, 0xEB, 0x00]))
    assert data["message_bits"] == 48
    assert data["padding_bits"] == 0

    fields = {f["name"]: f for f in data["fields"]}
    assert fields["temperature"]["value"] == 23.5
    assert fields["temperature"]["raw_value"] == 235
    assert fields["crc"]["value"] == crc16_ccitt(bytes([0xAA, 0x10, 0xEB, 0x00]))
    assert "humidity" not in fields

    branch = {b["field"]: b for b in data["branch_path"]}
    assert branch["temperature"]["matched"] is True
    assert branch["temperature"]["action"] == "encoded"
    assert branch["humidity"]["matched"] is False
    assert branch["humidity"]["action"] == "skipped"
    assert data["checksum"]["calculated_hex"] == "0x" + data["hex"][-4:]

    # the encoded payload parses back to the same values
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "temp_sensor", "version": "1.0.0", "hex": data["hex"]},
    )
    assert resp.status_code == 200
    parsed = {f["name"]: f for f in resp.json()["fields"]}
    assert parsed["temperature"]["value"] == 23.5
    assert resp.json()["checksum"]["valid"] is True


def test_encode_humidity_branch():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 2, "flags": 0, "humidity": 0x55},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["hex"] == make_message(bytes([0xAA, 0x20, 0x55]))
    names = [f["name"] for f in data["fields"]]
    assert "temperature" not in names  # skipped branch not required, not encoded


def test_encode_negative_temperature_two_complement():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": -5.0},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # raw = -50 -> two's complement 0xFFCE, little-endian -> CE FF
    assert data["hex"] == make_message(bytes([0xAA, 0x10, 0xCE, 0xFF]))
    resp = client.post(
        "/api/v1/parse", json={"device_type": "temp_sensor", "hex": data["hex"]}
    )
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["temperature"]["value"] == -5.0
    assert fields["temperature"]["raw_value"] == -50


def test_encode_missing_matched_field_422():
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "temp_sensor", "values": {"header": 0xAA, "msg_type": 2, "flags": 0}},
    )
    assert resp.status_code == 422
    assert "humidity" in resp.json()["detail"]


def test_encode_unknown_field_422():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.5, "ghost": 1},
        },
    )
    assert resp.status_code == 422
    assert "ghost" in resp.json()["detail"]


def test_encode_value_for_skipped_field_422():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.5, "humidity": 5},
        },
    )
    assert resp.status_code == 422
    assert "humidity" in resp.json()["detail"]


def test_encode_checksum_field_not_settable():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.5, "crc": 1},
        },
    )
    assert resp.status_code == 422
    assert "crc" in resp.json()["detail"]


def test_encode_out_of_range_422():
    # header is uint8, 0x1AA = 426 does not fit
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0x1AA, "msg_type": 1, "flags": 0, "temperature": 23.5},
        },
    )
    assert resp.status_code == 422


def test_encode_scaled_value_not_representable_422():
    # scale 0.1 -> 23.55 has no exact raw
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 23.55},
        },
    )
    assert resp.status_code == 422


def test_encode_scaled_raw_out_of_range_422():
    # 4000.0 -> raw 40000 exceeds int16 range
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": 0xAA, "msg_type": 1, "flags": 0, "temperature": 4000.0},
        },
    )
    assert resp.status_code == 422


def test_encode_wrong_value_type_422():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": "0xAA", "msg_type": 1, "flags": 0, "temperature": 23.5},
        },
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "temp_sensor",
            "values": {"header": True, "msg_type": 1, "flags": 0, "temperature": 23.5},
        },
    )
    assert resp.status_code == 422


def test_encode_unknown_device_404():
    resp = client.post("/api/v1/encode", json={"device_type": "ghost", "values": {}})
    assert resp.status_code == 404


def test_encode_unaligned_checksum_422():
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "nibble_dev",
            "version": "1.0.0",
            "fields": [
                {"name": "tag", "type": "uint", "bit_length": 4},
                {"name": "crc", "type": "uint", "bit_length": 8},
            ],
            "checksum": {"field": "crc", "algorithm": "sum8"},
        },
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/api/v1/encode", json={"device_type": "nibble_dev", "values": {"tag": 5}}
    )
    assert resp.status_code == 422
    assert "byte boundary" in resp.json()["detail"]


def test_encode_bool_hex_ascii_non_byte_aligned():
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "mixed_dev",
            "version": "1.0.0",
            "fields": [
                {"name": "enabled", "type": "bool", "bit_length": 1},
                {"name": "code", "type": "uint", "bit_length": 7},
                {"name": "magic", "type": "hex", "bit_length": 16},
                {"name": "label", "type": "ascii", "bit_length": 16},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "mixed_dev",
            "values": {"enabled": True, "code": 0x55, "magic": "0xABCD", "label": "OK"},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 1|1010101 -> 0xD5, then magic and label bytes
    assert data["hex"] == "d5abcd4f4b"
    assert data["checksum"] is None

    resp = client.post("/api/v1/parse", json={"device_type": "mixed_dev", "hex": data["hex"]})
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["enabled"]["value"] is True
    assert fields["code"]["value"] == 0x55
    assert fields["magic"]["value"] == "0xabcd"
    assert fields["label"]["value"] == "OK"


def test_encode_condition_uses_raw_values():
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "raw_cond",
            "version": "1.0.0",
            "fields": [
                {"name": "temp", "type": "int", "bit_length": 16, "scale": 0.1},
                {
                    "name": "alarm",
                    "type": "uint",
                    "bit_length": 8,
                    "condition": {"field": "temp", "op": "ge", "value": 1000},
                },
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    # 99.9 -> raw 999 < 1000 -> alarm skipped, must not be provided
    resp = client.post(
        "/api/v1/encode", json={"device_type": "raw_cond", "values": {"temp": 99.9}}
    )
    assert resp.status_code == 200, resp.text
    assert [f["name"] for f in resp.json()["fields"]] == ["temp"]

    # 100.0 -> raw 1000 >= 1000 -> alarm required
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "raw_cond", "values": {"temp": 100.0, "alarm": 1}},
    )
    assert resp.status_code == 200, resp.text
    assert [f["name"] for f in resp.json()["fields"]] == ["temp", "alarm"]

    resp = client.post(
        "/api/v1/encode", json={"device_type": "raw_cond", "values": {"temp": 100.0}}
    )
    assert resp.status_code == 422


def test_encode_signed_int_range():
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "signed_dev",
            "version": "1.0.0",
            "fields": [{"name": "delta", "type": "int", "bit_length": 8}],
        },
    )
    assert resp.status_code == 201, resp.text

    def enc(value):
        return client.post(
            "/api/v1/encode", json={"device_type": "signed_dev", "values": {"delta": value}}
        )

    resp = enc(-1)
    assert resp.status_code == 200, resp.text
    assert resp.json()["hex"] == "ff"  # two's complement

    assert enc(127).json()["hex"] == "7f"
    assert enc(-128).json()["hex"] == "80"
    assert enc(128).status_code == 422
    assert enc(-129).status_code == 422


def test_encode_version_rules_match_parse():
    for version, bits in (("1.0.0", 8), ("2.0.0", 16)):
        resp = client.post(
            "/api/v1/definitions",
            json={
                "device_type": "enc_dev",
                "version": version,
                "fields": [{"name": "val", "type": "uint", "bit_length": bits}],
            },
        )
        assert resp.status_code == 201, resp.text

    # latest active (2.0.0) used by default
    resp = client.post(
        "/api/v1/encode", json={"device_type": "enc_dev", "values": {"val": 0x1234}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == "2.0.0"
    assert resp.json()["hex"] == "1234"

    resp = client.post("/api/v1/definitions/enc_dev/2.0.0/deactivate")
    assert resp.status_code == 200

    # falls back to the still-active 1.0.0
    resp = client.post(
        "/api/v1/encode", json={"device_type": "enc_dev", "values": {"val": 0x12}}
    )
    assert resp.json()["version"] == "1.0.0"
    assert resp.json()["hex"] == "12"

    # explicit inactive version still encodable
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "enc_dev", "version": "2.0.0", "values": {"val": 0x1234}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["definition_active"] is False
    assert resp.json()["hex"] == "1234"


# ------------------------------------------------------------- version detect --


def _register_detect_version(device_type, version, fields, checksum=None):
    payload = {"device_type": device_type, "version": version, "fields": fields}
    if checksum is not None:
        payload["checksum"] = checksum
    resp = client.post("/api/v1/definitions", json=payload)
    assert resp.status_code == 201, resp.text


def test_detect_unique_candidate():
    # 1.0.0: [a, sum8] -> 2 bytes; 2.0.0: [a, b, crc16-ccitt] -> 4 bytes
    _register_detect_version(
        "detect_sensor",
        "1.0.0",
        [
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "cs", "type": "uint", "bit_length": 8},
        ],
        {"field": "cs", "algorithm": "sum8"},
    )
    _register_detect_version(
        "detect_sensor",
        "2.0.0",
        [
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "b", "type": "uint", "bit_length": 8},
            {"name": "crc", "type": "uint", "bit_length": 16},
        ],
        {"field": "crc", "algorithm": "crc16-ccitt"},
    )

    body = bytes([0xAA, 0x2A])
    msg = (body + crc16_ccitt(body).to_bytes(2, "big")).hex()
    resp = client.post("/api/v1/detect", json={"device_type": "detect_sensor", "hex": msg})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version"] == "2.0.0"
    assert data["device_type"] == "detect_sensor"
    assert data["definition_active"] is True
    assert data["consumed_bits"] == 32
    assert data["trailing_bits"] == 0
    assert data["checksum"]["valid"] is True
    assert {f["name"] for f in data["fields"]} == {"a", "b", "crc"}


def test_detect_inactive_version_still_detected():
    # the 2-byte sum8 message only fits 1.0.0, which is now deactivated
    resp = client.post("/api/v1/definitions/detect_sensor/1.0.0/deactivate")
    assert resp.status_code == 200

    msg = bytes([0xAB, 0xAB]).hex()  # sum8([0xAB]) == 0xAB
    resp = client.post("/api/v1/detect", json={"device_type": "detect_sensor", "hex": msg})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version"] == "1.0.0"
    assert data["definition_active"] is False
    assert data["checksum"]["valid"] is True


def test_detect_no_candidate_422_lists_reasons():
    # 1-byte message is too short for every version
    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_sensor", "hex": "AA"}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["device_type"] == "detect_sensor"
    failures = {f["version"]: f for f in detail["failures"]}
    assert set(failures) == {"1.0.0", "2.0.0"}
    assert "too short" in failures["1.0.0"]["reason"]
    assert "too short" in failures["2.0.0"]["reason"]
    # inactive flag is reported per version as well
    assert failures["1.0.0"]["active"] is False
    assert failures["2.0.0"]["active"] is True

    # well-sized for 1.0.0 but wrong checksum; still too short for 2.0.0
    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_sensor", "hex": "AA00"}
    )
    assert resp.status_code == 422
    reasons = {f["version"]: f["reason"] for f in resp.json()["detail"]["failures"]}
    assert "checksum mismatch" in reasons["1.0.0"]
    assert "too short" in reasons["2.0.0"]


def test_detect_multiple_candidates_409():
    # two registered versions with the exact same wire format and checksum
    for version in ("1.0.0", "2.0.0"):
        _register_detect_version(
            "detect_dup",
            version,
            [
                {"name": "a", "type": "uint", "bit_length": 8},
                {"name": "cs", "type": "uint", "bit_length": 8},
            ],
            {"field": "cs", "algorithm": "sum8"},
        )

    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_dup", "hex": "3c3c"}
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert isinstance(detail, dict)
    # candidates are listed in version order; latest is never picked silently
    assert [c["version"] for c in detail["candidates"]] == ["1.0.0", "2.0.0"]
    assert all(c["active"] is True for c in detail["candidates"])
    assert "fields" not in detail

    # ambiguity persists even after one version is deactivated
    client.post("/api/v1/definitions/detect_dup/2.0.0/deactivate")
    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_dup", "hex": "3c3c"}
    )
    assert resp.status_code == 409
    candidates = {c["version"]: c["active"] for c in resp.json()["detail"]["candidates"]}
    assert candidates == {"1.0.0": True, "2.0.0": False}


def test_detect_without_checksum_uses_full_consumption_only():
    _register_detect_version(
        "detect_nocs",
        "1.0.0",
        [
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "b", "type": "uint", "bit_length": 8},
        ],
    )
    _register_detect_version(
        "detect_nocs",
        "2.0.0",
        [
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "b", "type": "uint", "bit_length": 8},
            {"name": "c", "type": "uint", "bit_length": 8},
        ],
    )

    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_nocs", "hex": "0x01-02"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == "1.0.0"  # cleaned hex, no checksum needed

    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_nocs", "hex": "010203"}
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == "2.0.0"

    # trailing bits for both versions -> no candidate
    resp = client.post(
        "/api/v1/detect", json={"device_type": "detect_nocs", "hex": "01020304"}
    )
    assert resp.status_code == 422
    reasons = {f["version"]: f["reason"] for f in resp.json()["detail"]["failures"]}
    assert set(reasons) == {"1.0.0", "2.0.0"}
    assert all("trailing" in r for r in reasons.values())


def test_detect_unknown_device_invalid_hex_and_version_hint():
    assert (
        client.post("/api/v1/detect", json={"device_type": "ghost", "hex": "AABB"}).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/detect", json={"device_type": "detect_nocs", "hex": "zzzz"}
        ).status_code
        == 422
    )
    # the request must contain only device_type and hex: no version hint allowed
    resp = client.post(
        "/api/v1/detect",
        json={"device_type": "detect_nocs", "hex": "0102", "version": "1.0.0"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------- version compatibility --


def _compat(device_type, old, new):
    return client.post(
        f"/api/v1/definitions/{device_type}/compatibility",
        json={"old_version": old, "new_version": new},
    )


def test_compatibility_identical_structure_compatible():
    # temp_sensor 1.0.0 vs a re-registered copy under a new version
    payload = dict(SENSOR_DEF)
    payload["version"] = "1.1.0"
    payload["description"] = "same wire format, new docs"
    resp = client.post("/api/v1/definitions", json=payload)
    assert resp.status_code == 201, resp.text

    resp = _compat("temp_sensor", "1.0.0", "1.1.0")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["compatible"] is True
    assert data["status"] == "compatible"
    assert data["breaking_branches"] == 0
    # msg_type is a 4-bit control field cut at 1,2 -> 4 equivalence classes
    assert data["combination_count"] == 4
    assert data["reachable_branches"] == 4
    assert data["equivalence_classes"][0]["field"] == "msg_type"
    assert data["equivalence_classes"][0]["representatives"] == [0, 1, 2, 3]

    # every enumerated branch lays out fields with offsets and a total length
    for branch in data["branches"]:
        assert branch["compatible"] is True
        layout = branch["old_layout"]
        assert layout["total_bits"] == branch["new_layout"]["total_bits"]
        assert layout["checksum_algorithm"] == "crc16-ccitt"
        offsets = [f["bit_offset"] for f in layout["fields"]]
        assert offsets == sorted(offsets)
        assert all(f["bit_length"] > 0 for f in layout["fields"])

    # the temperature branch (msg_type == 1) layout
    temp_branch = next(
        b for b in data["branches"]
        if b["representative"]["msg_type"]["value"] == 1
    )
    names = [f["name"] for f in temp_branch["old_layout"]["fields"]]
    assert names == ["header", "msg_type", "flags", "temperature", "crc"]
    temp_field = next(f for f in temp_branch["old_layout"]["fields"] if f["name"] == "temperature")
    assert (temp_field["type"], temp_field["bit_length"], temp_field["bit_offset"]) == (
        "int", 16, 16,
    )
    assert temp_branch["old_layout"]["total_bits"] == 48


def test_compatibility_field_width_change_is_breaking():
    payload = {
        "device_type": "compat_w",
        "version": "1.0.0",
        "fields": [
            {"name": "t", "type": "uint", "bit_length": 4},
            {"name": "v", "type": "uint", "bit_length": 8,
             "condition": {"field": "t", "op": "eq", "value": 1}},
        ],
    }
    assert client.post("/api/v1/definitions", json=payload).status_code == 201

    new = {
        "device_type": "compat_w",
        "version": "2.0.0",
        "fields": [
            {"name": "t", "type": "uint", "bit_length": 4},
            {"name": "v", "type": "uint", "bit_length": 16,
             "condition": {"field": "t", "op": "eq", "value": 1}},
        ],
    }
    assert client.post("/api/v1/definitions", json=new).status_code == 201

    resp = _compat("compat_w", "1.0.0", "2.0.0")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["compatible"] is False
    assert data["status"] == "breaking"
    # t == 1 isolates a point, leaving one class above it -> 3 classes total;
    # only the t == 1 branch contains v.
    assert data["combination_count"] == 3
    assert data["breaking_branches"] == 1

    bad = next(
        b for b in data["branches"]
        if b["representative"]["t"]["value"] == 1
    )
    assert bad["compatible"] is False
    kinds = {d["kind"]: d for d in bad["differences"]}
    assert kinds["field_changed"]["field"] == "v"
    assert kinds["field_changed"]["bit_length"] == {"old": 8, "new": 16}
    assert kinds["total_length_changed"]["old"] == 12
    assert kinds["total_length_changed"]["new"] == 20

    # the other branches (t != 1) are unaffected and remain compatible
    assert all(b["compatible"] for b in data["branches"]
               if b["representative"]["t"]["value"] != 1)


def test_compatibility_checksum_algorithm_change_is_breaking():
    payload = {
        "device_type": "compat_crc",
        "version": "1.0.0",
        "fields": [{"name": "crc", "type": "uint", "bit_length": 16}],
        "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
    }
    assert client.post("/api/v1/definitions", json=payload).status_code == 201
    new = dict(payload)
    new["version"] = "2.0.0"
    new["checksum"] = {"field": "crc", "algorithm": "crc16-modbus"}
    assert client.post("/api/v1/definitions", json=new).status_code == 201

    resp = _compat("compat_crc", "1.0.0", "2.0.0")
    data = resp.json()
    assert data["compatible"] is False
    assert data["combination_count"] == 1  # no control fields -> one layout
    diff = data["branches"][0]["differences"][0]
    assert diff == {
        "kind": "checksum_algorithm_changed",
        "old": "crc16-ccitt",
        "new": "crc16-modbus",
    }
    assert data["branches"][0]["old_layout"]["checksum_algorithm"] == "crc16-ccitt"


def test_compatibility_new_version_adds_finer_branch_is_breaking_only_there():
    base = {
        "device_type": "compat_fine",
        "version": "1.0.0",
        "fields": [
            {"name": "t", "type": "uint", "bit_length": 4},
            {"name": "v", "type": "uint", "bit_length": 8,
             "condition": {"field": "t", "op": "eq", "value": 1}},
        ],
    }
    assert client.post("/api/v1/definitions", json=base).status_code == 201
    new = {
        "device_type": "compat_fine",
        "version": "2.0.0",
        "fields": [
            {"name": "t", "type": "uint", "bit_length": 4},
            {"name": "v", "type": "uint", "bit_length": 8,
             "condition": {"field": "t", "op": "eq", "value": 1}},
            {"name": "w", "type": "uint", "bit_length": 8,
             "condition": {"field": "t", "op": "eq", "value": 2}},
        ],
    }
    assert client.post("/api/v1/definitions", json=new).status_code == 201

    resp = _compat("compat_fine", "1.0.0", "2.0.0")
    data = resp.json()
    # eq 1 isolates one point and eq 2 another -> joint partition:
    # [0], [1], [2], [3..15]
    assert data["combination_count"] == 4
    assert data["breaking_branches"] == 1
    bad = next(
        b for b in data["branches"]
        if b["representative"]["t"]["value"] == 2
    )
    assert bad["compatible"] is False
    assert any(d["kind"] == "field_added" and d["field"] == "w" for d in bad["differences"])
    assert {
        b["representative"]["t"]["value"]
        for b in data["branches"] if b["compatible"]
    } == {0, 1, 3}


def test_compatibility_versions_missing_404_and_same_version_422():
    resp = _compat("compat_missing", "1.0.0", "2.0.0")
    assert resp.status_code == 404

    resp = _compat("temp_sensor", "1.0.0", "9.9.9")
    assert resp.status_code == 404

    resp = _compat("temp_sensor", "1.0.0", "1.0.0")
    assert resp.status_code == 422


def test_compatibility_too_many_combinations_422():
    # two independent uint9 control fields, each split into 17 classes -> 289
    def payload(version):
        fields = [{"name": "a", "type": "uint", "bit_length": 9}]
        for i in range(16):
            fields.append(
                {"name": f"x{i}", "type": "uint", "bit_length": 8,
                 "condition": {"field": "a", "op": "lt", "value": i + 1}}
            )
        fields.append({"name": "b", "type": "uint", "bit_length": 9})
        for i in range(16):
            fields.append(
                {"name": f"y{i}", "type": "uint", "bit_length": 8,
                 "condition": {"field": "b", "op": "lt", "value": i + 1}}
            )
        return {"device_type": "compat_big", "version": version, "fields": fields}

    assert client.post("/api/v1/definitions", json=payload("1.0.0")).status_code == 201
    assert client.post("/api/v1/definitions", json=payload("2.0.0")).status_code == 201

    resp = _compat("compat_big", "1.0.0", "2.0.0")
    assert resp.status_code == 422
    assert "256" in resp.json()["detail"]


def test_compatibility_nested_conditional_unreachable_combinations_skipped():
    def payload(version):
        return {
            "device_type": "compat_nested",
            "version": version,
            "fields": [
                {"name": "t", "type": "uint", "bit_length": 4},
                {"name": "c1", "type": "uint", "bit_length": 8,
                 "condition": {"field": "t", "op": "eq", "value": 1}},
                # c2's control field c1 is itself conditional: when t != 1 the
                # branch is unreachable and must not be compared.
                {"name": "c2", "type": "uint", "bit_length": 8,
                 "condition": {"field": "c1", "op": "gt", "value": 5}},
            ],
        }

    assert client.post("/api/v1/definitions", json=payload("1.0.0")).status_code == 201
    assert client.post("/api/v1/definitions", json=payload("2.0.0")).status_code == 201

    resp = _compat("compat_nested", "1.0.0", "2.0.0")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # t partition [0],[1],[2..15] crossed with c1 classes {..5},{6..} = 6
    # combos; only the t==1 ones are reachable (c1 is absent otherwise).
    assert data["combination_count"] == 6
    assert data["reachable_branches"] == 2
    assert data["unreachable_combinations"] == 4
    assert data["compatible"] is True
    # only reachable combos (t == 1, c1 present) appear in the branch list
    reps = {
        (b["representative"]["t"]["value"], b["representative"]["c1"]["value"])
        for b in data["branches"]
    }
    assert reps == {(1, 0), (1, 6)}
    c2_branch = next(
        b for b in data["branches"]
        if b["representative"]["t"]["value"] == 1
        and b["representative"]["c1"]["value"] == 6
    )
    assert [f["name"] for f in c2_branch["old_layout"]["fields"]] == ["t", "c1", "c2"]
    assert c2_branch["old_layout"]["total_bits"] == 20


# ------------------------------------------------------------------ repeats --


REPEAT_DEF = {
    "device_type": "array_dev",
    "version": "1.0.0",
    "description": "传感器采样数组报文",
    "fields": [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "count", "type": "uint", "bit_length": 8},
        {
            "name": "samples",
            "type": "uint",
            "bit_length": 16,
            "endianness": "little",
            "scale": 0.1,
            "repeat": {"field": "count", "max": 8},
        },
        {"name": "crc", "type": "uint", "bit_length": 16},
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
}


def _register_repeat():
    resp = client.post("/api/v1/definitions", json=REPEAT_DEF)
    assert resp.status_code == 201, resp.text


def _repeat_hex(samples):
    body = bytes([0xAA, len(samples)]) + b"".join(
        int(round(s * 10)).to_bytes(2, "little") for s in samples
    )
    return (body + crc16_ccitt(body).to_bytes(2, "big")).hex()


def test_register_repeat_validation():
    _register_repeat()

    def bad(fields, version):
        return client.post(
            "/api/v1/definitions",
            json={"device_type": "rep_bad", "version": version, "fields": fields},
        )

    # counter must be an earlier scalar uint field
    assert bad(
        [{"name": "a", "type": "int", "bit_length": 8},
         {"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "a", "max": 4}}],
        "1.0.0",
    ).status_code == 422
    # forward / unknown reference
    assert bad(
        [{"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "ghost", "max": 4}}],
        "2.0.0",
    ).status_code == 422
    # self reference
    assert bad(
        [{"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "r", "max": 4}}],
        "3.0.0",
    ).status_code == 422
    # max out of [1, 64]
    assert bad(
        [{"name": "a", "type": "uint", "bit_length": 8},
         {"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "a", "max": 0}}],
        "4.0.0",
    ).status_code == 422
    assert bad(
        [{"name": "a", "type": "uint", "bit_length": 8},
         {"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "a", "max": 65}}],
        "5.0.0",
    ).status_code == 422
    # a repeated field cannot drive later conditions
    assert bad(
        [
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "a", "max": 4}},
            {"name": "x", "type": "uint", "bit_length": 8,
             "condition": {"field": "r", "op": "eq", "value": 1}},
        ],
        "6.0.0",
    ).status_code == 422
    # ... nor serve as another counter
    assert bad(
        [
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "r", "type": "uint", "bit_length": 8, "repeat": {"field": "a", "max": 4}},
            {"name": "r2", "type": "uint", "bit_length": 8, "repeat": {"field": "r", "max": 4}},
        ],
        "7.0.0",
    ).status_code == 422
    # the checksum field itself must not be repeated
    resp = client.post(
        "/api/v1/definitions",
        json={
            "device_type": "rep_bad",
            "version": "8.0.0",
            "fields": [
                {"name": "a", "type": "uint", "bit_length": 8},
                {"name": "cs", "type": "uint", "bit_length": 8,
                 "repeat": {"field": "a", "max": 4}},
            ],
            "checksum": {"field": "cs", "algorithm": "sum8"},
        },
    )
    assert resp.status_code == 422


def test_parse_repeated_field_arrays_and_offsets():
    samples = [23.2, 200.0, 1000.0]  # raws 232, 2000, 10000
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "array_dev", "version": "1.0.0", "hex": _repeat_hex(samples)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    fields = {f["name"]: f for f in data["fields"]}

    rep = fields["samples"]
    assert rep["bit_offset"] == 16
    assert rep["bit_length"] == 16  # per-element width
    assert rep["count"] == 3
    assert rep["value"] == samples
    assert rep["raw_value"] == [232, 2000, 10000]

    # repeats push later fields and the checksum position forward
    assert fields["crc"]["bit_offset"] == 16 + 3 * 16
    assert data["consumed_bits"] == 16 + 3 * 16 + 16
    assert data["trailing_bits"] == 0
    assert data["checksum"]["valid"] is True
    assert data["checksum"]["range"] == f"bytes [0, {fields['crc']['bit_offset'] // 8})"


def test_parse_repeated_zero_count():
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "array_dev", "version": "1.0.0", "hex": _repeat_hex([])},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    fields = {f["name"]: f for f in data["fields"]}
    rep = fields["samples"]
    assert rep["count"] == 0
    assert rep["value"] == []
    assert rep["raw_value"] == []
    assert rep["bit_offset"] == 16
    assert fields["crc"]["bit_offset"] == 16  # nothing expanded
    assert data["consumed_bits"] == 32
    assert data["checksum"]["valid"] is True


def test_parse_repeat_count_over_max_422():
    # count byte = 9 > max 8; payload deliberately short as well
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "array_dev", "version": "1.0.0", "hex": "aa09"},
    )
    assert resp.status_code == 422
    assert "exceeds the declared maximum" in resp.json()["detail"]


def test_parse_repeat_message_too_short_422():
    # count = 2 but no sample bytes follow
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "array_dev", "version": "1.0.0", "hex": "aa023412"},
    )
    assert resp.status_code == 422
    assert "message too short" in resp.json()["detail"]


def test_parse_repeat_counter_skipped_by_branch_422():
    payload = {
        "device_type": "rep_skip",
        "version": "1.0.0",
        "fields": [
            {"name": "kind", "type": "uint", "bit_length": 8},
            {"name": "n", "type": "uint", "bit_length": 8,
             "condition": {"field": "kind", "op": "eq", "value": 1}},
            {"name": "tags", "type": "uint", "bit_length": 8,
             "repeat": {"field": "n", "max": 4}},
        ],
    }
    assert client.post("/api/v1/definitions", json=payload).status_code == 201
    # kind != 1 -> counter n absent -> repeat cannot be resolved
    resp = client.post(
        "/api/v1/parse", json={"device_type": "rep_skip", "hex": "00"}
    )
    assert resp.status_code == 422
    assert "repeat counter 'n' was not parsed" in resp.json()["detail"]


def test_parse_repeat_behind_condition_not_expanded_when_missed():
    payload = {
        "device_type": "rep_cond",
        "version": "1.0.0",
        "fields": [
            {"name": "kind", "type": "uint", "bit_length": 8},
            {"name": "n", "type": "uint", "bit_length": 8},
            {"name": "tags", "type": "ascii", "bit_length": 8,
             "repeat": {"field": "n", "max": 4},
             "condition": {"field": "kind", "op": "eq", "value": 1}},
        ],
    }
    assert client.post("/api/v1/definitions", json=payload).status_code == 201

    # condition missed: n's value must not expand tags
    resp = client.post("/api/v1/parse", json={"device_type": "rep_cond", "hex": "0002"})
    assert resp.status_code == 200
    data = resp.json()
    assert [f["name"] for f in data["fields"]] == ["kind", "n"]
    assert data["consumed_bits"] == 16

    # condition hit: tags = ["A", "B"]
    resp = client.post("/api/v1/parse", json={"device_type": "rep_cond", "hex": "01024142"})
    assert resp.status_code == 200
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["tags"]["value"] == ["A", "B"]
    assert fields["tags"]["count"] == 2


def test_encode_repeated_field_roundtrip():
    samples = [23.2, 200.0, 1000.0]
    expected_hex = _repeat_hex(samples)
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "array_dev",
            "version": "1.0.0",
            "values": {"header": 0xAA, "count": 3, "samples": samples},
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["hex"] == expected_hex
    fields = {f["name"]: f for f in data["fields"]}
    assert fields["samples"]["value"] == samples
    assert fields["samples"]["raw_value"] == [232, 2000, 10000]
    assert fields["samples"]["count"] == 3
    assert fields["crc"]["bit_offset"] == 64

    # parses back identically
    resp = client.post(
        "/api/v1/parse",
        json={"device_type": "array_dev", "version": "1.0.0", "hex": data["hex"]},
    )
    assert resp.status_code == 200
    rep = {f["name"]: f for f in resp.json()["fields"]}["samples"]
    assert rep["value"] == samples
    assert resp.json()["checksum"]["valid"] is True


def test_encode_repeated_zero_count_requires_empty_list():
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "array_dev", "values": {"header": 0xAA, "count": 0, "samples": []}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["hex"] == _repeat_hex([])

    # count == 0 but a non-empty list is supplied
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "array_dev",
              "values": {"header": 0xAA, "count": 0, "samples": [1.0]}},
    )
    assert resp.status_code == 422
    assert "exactly 0" in resp.json()["detail"]


def test_encode_repeated_array_length_mismatch_422():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "array_dev",
            "values": {"header": 0xAA, "count": 3, "samples": [1.0, 2.0]},
        },
    )
    assert resp.status_code == 422
    assert "exactly 3" in resp.json()["detail"]


def test_encode_repeated_value_must_be_list_422():
    resp = client.post(
        "/api/v1/encode",
        json={"device_type": "array_dev",
              "values": {"header": 0xAA, "count": 3, "samples": 1.0}},
    )
    assert resp.status_code == 422
    assert "expected a list" in resp.json()["detail"]


def test_encode_repeated_element_type_and_range_422():
    # one element is a string where an integer/float is required
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "array_dev",
            "values": {"header": 0xAA, "count": 2, "samples": [1.0, "nope"]},
        },
    )
    assert resp.status_code == 422
    assert "samples[1]" in resp.json()["detail"]

    # one element's raw value does not fit in uint16
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "array_dev",
            "values": {"header": 0xAA, "count": 1, "samples": [70000.0]},
        },
    )
    assert resp.status_code == 422
    assert "samples[0]" in resp.json()["detail"]


def test_encode_repeat_count_over_max_422():
    resp = client.post(
        "/api/v1/encode",
        json={
            "device_type": "array_dev",
            "values": {"header": 0xAA, "count": 9, "samples": [0.0] * 9},
        },
    )
    assert resp.status_code == 422
    assert "exceeds the declared maximum" in resp.json()["detail"]


def test_compatibility_rejects_repeat_definitions_422():
    # array_dev 1.0.0 carries a repeat declaration
    payload = dict(REPEAT_DEF)
    payload["version"] = "2.0.0"
    assert client.post("/api/v1/definitions", json=payload).status_code == 201

    resp = _compat("array_dev", "1.0.0", "2.0.0")
    assert resp.status_code == 422
    assert "repeated" in resp.json()["detail"]

    # even comparing against a definition without repeats is refused when
    # either side contains one
    plain = {
        "device_type": "array_dev",
        "version": "3.0.0",
        "fields": [
            {"name": "header", "type": "uint", "bit_length": 8},
            {"name": "count", "type": "uint", "bit_length": 8},
            {"name": "crc", "type": "uint", "bit_length": 16},
        ],
        "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
    }
    assert client.post("/api/v1/definitions", json=plain).status_code == 201
    resp = _compat("array_dev", "3.0.0", "1.0.0")
    assert resp.status_code == 422
    assert "repeated" in resp.json()["detail"]


# ------------------------------------------------------- regression fixtures --


# v1: 4-byte body + crc16; v2: 5-byte body + crc16 (v1 cannot parse v2 messages)
FIX_SENSOR_V1 = {
    "device_type": "fix_sensor",
    "version": "1.0.0",
    "fields": [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "msg_type", "type": "uint", "bit_length": 8},
        {"name": "temperature", "type": "int", "bit_length": 16},
        {"name": "crc", "type": "uint", "bit_length": 16},
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
}
FIX_SENSOR_V2 = {
    "device_type": "fix_sensor",
    "version": "2.0.0",
    "fields": [
        {"name": "header", "type": "uint", "bit_length": 8},
        {"name": "msg_type", "type": "uint", "bit_length": 8},
        {"name": "temperature", "type": "int", "bit_length": 32},
        {"name": "crc", "type": "uint", "bit_length": 16},
    ],
    "checksum": {"field": "crc", "algorithm": "crc16-ccitt"},
}

# identical layout across versions, only endianness changes -> parses on both,
# but raw_value changes
FIX_METER_V1 = {
    "device_type": "fix_meter",
    "version": "1.0.0",
    "fields": [
        {"name": "kind", "type": "uint", "bit_length": 8},
        {"name": "value", "type": "uint", "bit_length": 16, "endianness": "big"},
    ],
}
FIX_METER_V2 = dict(FIX_METER_V1, version="2.0.0")
FIX_METER_V2 = {
    "device_type": "fix_meter",
    "version": "2.0.0",
    "fields": [
        {"name": "kind", "type": "uint", "bit_length": 8},
        {"name": "value", "type": "uint", "bit_length": 16, "endianness": "little"},
    ],
}

# branches change between versions
FIX_BRANCH_V1 = {
    "device_type": "fix_branch",
    "version": "1.0.0",
    "fields": [
        {"name": "kind", "type": "uint", "bit_length": 8},
        {
            "name": "alarm",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "kind", "op": "eq", "value": 1},
        },
    ],
}
FIX_BRANCH_V2 = {
    "device_type": "fix_branch",
    "version": "2.0.0",
    "fields": [
        {"name": "kind", "type": "uint", "bit_length": 8},
        {
            "name": "alarm",
            "type": "uint",
            "bit_length": 8,
            "condition": {"field": "kind", "op": "eq", "value": 2},
        },
    ],
}


def _setup_fixture_devices():
    for payload in (
        FIX_SENSOR_V1, FIX_SENSOR_V2, FIX_METER_V1, FIX_METER_V2,
        FIX_BRANCH_V1, FIX_BRANCH_V2,
    ):
        client.post("/api/v1/definitions", json=payload)


def _crc_msg(body: bytes) -> str:
    return (body + crc16_ccitt(body).to_bytes(2, "big")).hex()


def test_create_fixture_captures_baseline_snapshot():
    _setup_fixture_devices()
    hex_payload = _crc_msg(bytes([0xAA, 0x01, 0x00, 0xEB]))  # temp raw 235
    resp = client.post(
        "/api/v1/fixtures",
        json={
            "name": "temp-branch",
            "device_type": "fix_sensor",
            "version": "1.0.0",
            "hex": hex_payload,
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "temp-branch"
    assert data["device_type"] == "fix_sensor"
    assert data["version"] == "1.0.0"
    assert data["hex"] == hex_payload
    snap = data["snapshot"]
    fields = {f["name"]: f for f in snap["fields"]}
    assert fields["header"]["raw_value"] == 0xAA
    assert fields["temperature"]["raw_value"] == 235
    assert snap["checksum_valid"] is True
    assert snap["branch_path"] == []  # unconditional v1
    assert "created_at" in data


def test_create_fixture_name_unique_within_device_type_only():
    _setup_fixture_devices()
    # same name under another device type is fine
    resp = client.post(
        "/api/v1/fixtures",
        json={"name": "temp-branch", "device_type": "fix_meter",
              "version": "1.0.0", "hex": "011234"},
    )
    assert resp.status_code == 201, resp.text

    # duplicate name inside the same device type -> 409
    resp = client.post(
        "/api/v1/fixtures",
        json={"name": "temp-branch", "device_type": "fix_sensor",
              "version": "1.0.0", "hex": _crc_msg(bytes([0xAA, 0x01, 0x00, 0xEB]))},
    )
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


def test_create_fixture_missing_definition_404_and_bad_baseline_422():
    _setup_fixture_devices()
    resp = client.post(
        "/api/v1/fixtures",
        json={"name": "x", "device_type": "fix_sensor", "version": "9.9.9", "hex": "aabb"},
    )
    assert resp.status_code == 404

    # message too short for the referenced baseline definition
    resp = client.post(
        "/api/v1/fixtures",
        json={"name": "short", "device_type": "fix_sensor", "version": "1.0.0", "hex": "aa01"},
    )
    assert resp.status_code == 422
    assert "baseline parse failed" in resp.json()["detail"]

    # invalid hex
    resp = client.post(
        "/api/v1/fixtures",
        json={"name": "badhex", "device_type": "fix_meter", "version": "1.0.0", "hex": "zz"},
    )
    assert resp.status_code == 422


def test_create_fixture_with_expectation():
    _setup_fixture_devices()
    hex_payload = _crc_msg(bytes([0xAA, 0x01, 0x00, 0xEB]))

    # matching expectation
    resp = client.post(
        "/api/v1/fixtures",
        json={
            "name": "with-expect-ok",
            "device_type": "fix_sensor",
            "version": "1.0.0",
            "hex": hex_payload,
            "expect": {
                "fields": [
                    {"name": "header", "raw_value": 170},
                    {"name": "temperature"},  # presence-only assertion
                    {"name": "crc", "raw_value": crc16_ccitt(bytes([0xAA, 1, 0, 0xEB]))},
                ],
                "checksum_valid": True,
            },
        },
    )
    assert resp.status_code == 201, resp.text
    # the full snapshot is persisted regardless of the partial expectation
    assert len(resp.json()["snapshot"]["fields"]) == 4

    # mismatching raw_value -> 422 with per-field problems, fixture not stored
    resp = client.post(
        "/api/v1/fixtures",
        json={
            "name": "with-expect-bad",
            "device_type": "fix_sensor",
            "version": "1.0.0",
            "hex": hex_payload,
            "expect": {"fields": [{"name": "header", "raw_value": 1}]},
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["message"] == "fixture expectation does not match baseline"
    assert any("'header'" in p for p in detail["problems"])

    # expectation referencing a field that the baseline never parses
    resp = client.post(
        "/api/v1/fixtures",
        json={
            "name": "with-expect-ghost",
            "device_type": "fix_sensor",
            "version": "1.0.0",
            "hex": hex_payload,
            "expect": {"fields": [{"name": "humidity", "raw_value": 5}]},
        },
    )
    assert resp.status_code == 422
    assert "not parsed" in resp.json()["detail"]["problems"][0]


def test_run_fixture_against_baseline_passes():
    _setup_fixture_devices()
    resp = client.post("/api/v1/fixtures/temp-branch/run",
                       json={"device_type": "fix_sensor"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["baseline_version"] == "1.0.0"
    assert data["target_version"] == "1.0.0"
    assert data["version_changed"] is False
    assert data["parse_error"] is None
    assert data["passed"] is True
    cmp_ = data["comparison"]
    assert cmp_["passed"] is True
    assert cmp_["fields"]["passed"] is True
    assert cmp_["branch_path"]["passed"] is True
    assert cmp_["checksum"]["passed"] is True
    assert cmp_["pass_rate"] == 1.0
    assert cmp_["passed_checks"] == cmp_["total_checks"]
    assert cmp_["errors"] == []
    # four fields + no branches + one checksum check
    assert cmp_["total_checks"] == 5

    # only device_type given (target_version omitted)
    resp = client.post("/api/v1/fixtures/temp-branch/run",
                       json={"device_type": "fix_sensor"})
    assert resp.status_code == 200
    assert resp.json()["passed"] is True


def test_run_fixture_against_changed_version_parse_failure_keeps_item_errors():
    _setup_fixture_devices()
    # v2 widens temperature to 32 bits: the 6-byte v1 message is too short
    resp = client.post(
        "/api/v1/fixtures/temp-branch/run",
        json={"device_type": "fix_sensor", "target_version": "2.0.0"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version_changed"] is True
    assert data["target_version"] == "2.0.0"
    assert data["passed"] is False
    assert data["parse_error"] is not None
    assert "too short" in data["parse_error"]
    cmp_ = data["comparison"]
    # every baseline field is reported removed + parse error retained
    removed = {f["name"] for f in cmp_["fields"]["removed"]}
    assert removed == {"header", "msg_type", "temperature", "crc"}
    assert cmp_["fields"]["added"] == []
    assert cmp_["checksum"]["passed"] is False
    assert cmp_["passed_checks"] == 0
    assert cmp_["pass_rate"] == 0.0
    assert any(e.startswith("target parse failed") for e in cmp_["errors"])
    assert any("field 'header'" in e for e in cmp_["errors"])


def test_run_fixture_changed_raw_values_reports_diff_and_pass_rate():
    _setup_fixture_devices()
    resp = client.post("/api/v1/fixtures/temp-branch/run",
                       json={"device_type": "fix_sensor", "target_version": "9.9.9"})
    assert resp.status_code == 404  # missing target definition

    # fix_meter fixture "temp-branch": baseline big-endian 0x1234 = 4660
    resp = client.post("/api/v1/fixtures/temp-branch/run",
                       json={"device_type": "fix_meter"})
    assert resp.status_code == 200
    assert resp.json()["target_version"] == "1.0.0"

    resp = client.post(
        "/api/v1/fixtures/temp-branch/run",
        json={"device_type": "fix_meter", "target_version": "2.0.0"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version_changed"] is True
    assert data["parse_error"] is None
    cmp_ = data["comparison"]
    changed = {c["name"]: c for c in cmp_["fields"]["changed"]}
    assert set(changed) == {"value"}
    assert changed["value"]["baseline_raw_value"] == 0x1234
    assert changed["value"]["actual_raw_value"] == 0x3412
    names = {f["name"] for f in cmp_["fields"]["unchanged"]}
    assert names == {"kind"}
    assert cmp_["fields"]["added"] == [] and cmp_["fields"]["removed"] == []
    assert cmp_["checksum"]["passed"] is True  # no checksum on either version
    # 2 fields + 1 checksum check, one field changed -> 2/3
    assert cmp_["total_checks"] == 3
    assert cmp_["passed_checks"] == 2
    assert cmp_["pass_rate"] == round(2 / 3, 4)
    assert data["passed"] is False
    assert any("'value'" in e for e in cmp_["errors"])


def test_run_fixture_branch_path_changes():
    _setup_fixture_devices()
    # kind == 1 hits alarm on v1; on v2 the condition value is 2
    client.post(
        "/api/v1/fixtures",
        json={"name": "alarm-hit", "device_type": "fix_branch",
              "version": "1.0.0", "hex": "0105"},
    )
    resp = client.post("/api/v1/fixtures/alarm-hit/run",
                       json={"device_type": "fix_branch", "target_version": "2.0.0"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    cmp_ = data["comparison"]
    # v2 skips alarm when kind == 1: field disappears and branch flips
    assert {f["name"] for f in cmp_["fields"]["removed"]} == {"alarm"}
    assert {f["name"] for f in cmp_["fields"]["unchanged"]} == {"kind"}
    assert cmp_["branch_path"]["passed"] is False
    changed = {c["field"]: c for c in cmp_["branch_path"]["changed"]}
    assert set(changed) == {"alarm"}
    assert changed["alarm"]["baseline"]["matched"] is True
    assert changed["alarm"]["actual"]["matched"] is False
    assert changed["alarm"]["actual"]["action"] == "skipped"
    assert data["passed"] is False
    assert any("branch on 'alarm'" in e for e in cmp_["errors"])

    # re-running v2's taken branch (kind == 2, alarm skipped) captured from v1
    client.post(
        "/api/v1/fixtures",
        json={"name": "alarm-miss", "device_type": "fix_branch",
              "version": "1.0.0", "hex": "0209"},
    )
    resp = client.post("/api/v1/fixtures/alarm-miss/run",
                       json={"device_type": "fix_branch", "target_version": "2.0.0"})
    data = resp.json()
    cmp_ = data["comparison"]
    # alarm added (now parsed on v2) and its raw value appears
    assert {f["name"] for f in cmp_["fields"]["added"]} == {"alarm"}
    changed = {c["field"]: c for c in cmp_["branch_path"]["changed"]}
    assert changed["alarm"]["baseline"]["matched"] is False
    assert changed["alarm"]["actual"]["matched"] is True


def test_fixture_checksum_mismatch_is_reported():
    _setup_fixture_devices()
    body = bytes([0xAA, 0x01, 0x00, 0xEB])
    good = _crc_msg(body)
    bad = good[:-4] + "0000"  # corrupt crc
    resp = client.post(
        "/api/v1/fixtures",
        json={"name": "bad-crc", "device_type": "fix_sensor",
              "version": "1.0.0", "hex": bad},
    )
    assert resp.status_code == 201  # checksum mismatch is still a parse result
    snap = resp.json()["snapshot"]
    assert snap["checksum_valid"] is False

    resp = client.post("/api/v1/fixtures/bad-crc/run", json={"device_type": "fix_sensor"})
    data = resp.json()
    cmp_ = data["comparison"]
    assert cmp_["checksum"]["baseline_valid"] is False
    assert cmp_["checksum"]["actual_valid"] is False
    # bad vs baseline bad -> the fixture is internally consistent (and the
    # invalid checksum is faithfully captured in the baseline snapshot)
    assert cmp_["checksum"]["passed"] is True
    assert data["passed"] is True

    # a corrupt-crc fixture run against the good-payload expectation shows the
    # checksum-validation change explicitly
    client.post(
        "/api/v1/fixtures",
        json={"name": "good-crc", "device_type": "fix_sensor",
              "version": "1.0.0", "hex": good},
    )
    resp = client.post("/api/v1/fixtures/good-crc/run", json={"device_type": "fix_sensor"})
    assert resp.json()["passed"] is True

    # the comparison layer itself flags a checksum-result flip
    from app.fixtures import compare_snapshots

    flip = compare_snapshots(
        {"fields": [], "branch_path": [], "checksum_valid": True},
        {"fields": [], "branch_path": [], "checksum_valid": False},
    )
    assert flip["checksum"]["passed"] is False
    assert any("checksum validation changed" in e for e in flip["errors"])


def test_list_get_delete_fixtures():
    _setup_fixture_devices()
    resp = client.get("/api/v1/fixtures")
    assert resp.status_code == 200
    data = resp.json()
    pairs = {(i["device_type"], i["name"]) for i in data["items"]}
    assert ("fix_sensor", "temp-branch") in pairs
    assert ("fix_meter", "temp-branch") in pairs
    assert data["count"] == len(data["items"])

    resp = client.get("/api/v1/fixtures", params={"device_type": "fix_branch"})
    assert {i["name"] for i in resp.json()["items"]} == {"alarm-hit", "alarm-miss"}

    # unique name resolves directly
    resp = client.get("/api/v1/fixtures/alarm-hit")
    assert resp.status_code == 200
    assert resp.json()["device_type"] == "fix_branch"

    # ambiguous name across device types -> 409 with candidates
    resp = client.get("/api/v1/fixtures/temp-branch")
    assert resp.status_code == 409
    assert {c["device_type"] for c in resp.json()["detail"]["candidates"]} == {
        "fix_sensor", "fix_meter",
    }

    # unknown name -> 404
    assert client.get("/api/v1/fixtures/nope").status_code == 404
    assert client.post(
        "/api/v1/fixtures/nope/run", json={"device_type": "fix_sensor"}
    ).status_code == 404

    # delete: ambiguous -> 409, unique -> 204 then 404
    assert client.delete("/api/v1/fixtures/temp-branch").status_code == 409
    resp = client.delete("/api/v1/fixtures/alarm-hit")
    assert resp.status_code == 204
    assert resp.text == ""
    assert client.get("/api/v1/fixtures/alarm-hit").status_code == 404

    # the same-named fixture under fix_sensor is untouched
    assert client.get("/api/v1/fixtures/alarm-miss").status_code == 200


def test_fixture_hex_cleaning_and_body_validation():
    _setup_fixture_devices()
    # 0x prefix, separators and whitespace are cleaned like /parse
    resp = client.post(
        "/api/v1/fixtures",
        json={"name": "clean-hex", "device_type": "fix_meter",
              "version": "1.0.0", "hex": "0x01_12 34"},
    )
    assert resp.status_code == 201, resp.text
    resp = client.post("/api/v1/fixtures/clean-hex/run",
                       json={"device_type": "fix_meter"})
    assert resp.json()["passed"] is True

    # unknown body fields rejected
    resp = client.post(
        "/api/v1/fixtures/clean-hex/run",
        json={"device_type": "fix_meter", "target_version": "1.0.0", "extra": 1},
    )
    assert resp.status_code == 422

    # invalid fixture name pattern
    resp = client.post(
        "/api/v1/fixtures",
        json={"name": "bad name!", "device_type": "fix_meter",
              "version": "1.0.0", "hex": "011234"},
    )
    assert resp.status_code == 422


def test_fixture_run_requires_device_type_and_query_disambiguation():
    _setup_fixture_devices()
    # device_type is mandatory in the run body
    resp = client.post("/api/v1/fixtures/temp-branch/run", json={"target_version": "1.0.0"})
    assert resp.status_code == 422
    resp = client.post("/api/v1/fixtures/temp-branch/run", json={})
    assert resp.status_code == 422

    # GET / DELETE accept ?device_type= to disambiguate shared names
    resp = client.get("/api/v1/fixtures/temp-branch", params={"device_type": "fix_meter"})
    assert resp.status_code == 200
    assert resp.json()["device_type"] == "fix_meter"

    resp = client.delete("/api/v1/fixtures/temp-branch",
                         params={"device_type": "fix_meter"})
    assert resp.status_code == 204
    # sensor copy survives; meter copy is gone
    resp = client.get("/api/v1/fixtures/temp-branch", params={"device_type": "fix_meter"})
    assert resp.status_code == 404
    resp = client.get("/api/v1/fixtures/temp-branch", params={"device_type": "fix_sensor"})
    assert resp.status_code == 200
    # name is no longer ambiguous -> bare lookup works again
    resp = client.get("/api/v1/fixtures/temp-branch")
    assert resp.status_code == 200
    assert resp.json()["device_type"] == "fix_sensor"


def test_fixture_snapshot_covers_bool_and_repeated_fields():
    _setup_fixture_devices()
    # reuse the pre-registered array_dev definition (repeat) and a bool-bearing def
    payload = {
        "device_type": "fix_repeat",
        "version": "1.0.0",
        "fields": [
            {"name": "en", "type": "bool", "bit_length": 1},
            {"name": "n", "type": "uint", "bit_length": 7},
            {"name": "vals", "type": "uint", "bit_length": 8,
             "repeat": {"field": "n", "max": 4}},
        ],
    }
    client.post("/api/v1/definitions", json=payload)
    # en=1, n=2, vals=[0x11, 0x22] -> 1|0000010 = 0x82 then 11 22
    resp = client.post(
        "/api/v1/fixtures",
        json={"name": "rep", "device_type": "fix_repeat",
              "version": "1.0.0", "hex": "821122"},
    )
    assert resp.status_code == 201, resp.text
    snap = resp.json()["snapshot"]
    by_name = {f["name"]: f for f in snap["fields"]}
    assert by_name["en"]["raw_value"] == 1  # bool normalized
    assert by_name["n"]["raw_value"] == 2
    assert by_name["vals"]["raw_value"] == [0x11, 0x22]  # repeated -> list
    assert snap["checksum_valid"] is None  # no checksum configured

    resp = client.post("/api/v1/fixtures/rep/run",
                       json={"device_type": "fix_repeat"})
    assert resp.status_code == 200
    assert resp.json()["passed"] is True
    assert resp.json()["comparison"]["pass_rate"] == 1.0


# ------------------------------------------------------- byte-stream framing --


# Wire frame for stream_dev:
#   AA | length(1B, big) | seq(1B) | temperature(2B, big) | xor8(1B)
# length counts the bytes after the length field (seq + temp + xor = 4),
# so length_field_offset=1, length_field_bytes=1, length_adjustment=0 and
# the total frame length is 2 (sync+length) + length value = 6.
STREAM_DEF = {
    "device_type": "stream_dev",
    "version": "1.0.0",
    "fields": [
        {"name": "sync", "type": "uint", "bit_length": 8},
        {"name": "length", "type": "uint", "bit_length": 8},
        {"name": "seq", "type": "uint", "bit_length": 8},
        {"name": "temperature", "type": "int", "bit_length": 16},
        {"name": "xor", "type": "uint", "bit_length": 8},
    ],
    "checksum": {"field": "xor", "algorithm": "xor8"},
}

STREAM_PARAMS = {
    "sync_hex": "aa",
    "length_field_offset": 1,
    "length_field_bytes": 1,
    "length_endianness": "big",
    "length_adjustment": 0,
}


def _stream_frame(seq: int, temperature: int) -> bytes:
    # post-length bytes: seq(1) + temperature(2) + xor(1) = 4; total 6
    body = bytes([0xAA, 4, seq]) + temperature.to_bytes(2, "big", signed=True)
    return body + bytes([xor8(body)])


def _stream_request(chunks, **overrides):
    payload = {"device_type": "stream_dev", "version": "1.0.0",
               **STREAM_PARAMS, "chunks": list(chunks)}
    payload.update(overrides)
    return client.post("/api/v1/stream/parse", json=payload)


def test_stream_register():
    resp = client.post("/api/v1/definitions", json=STREAM_DEF)
    assert resp.status_code == 201, resp.text


def test_stream_single_frame_one_chunk():
    frame = _stream_frame(1, 25)
    resp = _stream_request([frame.hex()])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frame_count"] == 1
    assert data["received_bytes"] == len(frame)
    fr = data["frames"][0]
    assert (fr["start"], fr["end"], fr["length"]) == (0, 6, 6)
    assert fr["hex"] == frame.hex()
    fields = {f["name"]: f for f in fr["result"]["fields"]}
    assert fields["seq"]["value"] == 1
    assert fields["temperature"]["value"] == 25
    assert fr["result"]["checksum"]["valid"] is True
    assert data["discarded_ranges"] == []
    assert data["discarded_bytes"] == 0
    assert data["incomplete_hex"] == ""
    assert data["incomplete_bytes"] == 0


def test_stream_multiple_frames_with_offsets():
    f1 = _stream_frame(0, 100)
    f2 = _stream_frame(1, -5)
    noise = bytes([0x12, 0x34])
    stream = noise + f1 + noise + f2
    resp = _stream_request([stream.hex()])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frame_count"] == 2
    assert [(f["start"], f["end"]) for f in data["frames"]] == [
        (2, 8), (10, 16),
    ]
    fields1 = {f["name"]: f for f in data["frames"][0]["result"]["fields"]}
    fields2 = {f["name"]: f for f in data["frames"][1]["result"]["fields"]}
    assert fields1["temperature"]["value"] == 100
    assert fields2["temperature"]["value"] == -5
    discarded = data["discarded_ranges"]
    assert [(d["start"], d["end"], d["hex"]) for d in discarded] == [
        (0, 2, "1234"),
        (8, 10, "1234"),
    ]
    assert all(d["reason"] == REASON_NOISE for d in discarded)
    assert data["discarded_bytes"] == 4
    assert data["incomplete_hex"] == ""


def test_stream_frame_split_across_chunks():
    frame = _stream_frame(7, 200)
    # split at every byte position; also feed the sync byte alone first
    pieces = [frame[i:i + 1].hex() for i in range(len(frame))]
    resp = _stream_request(pieces)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frame_count"] == 1
    assert (data["frames"][0]["start"], data["frames"][0]["end"]) == (0, 6)
    assert data["incomplete_hex"] == ""

    # frame split in the middle of the length/body boundary, across two chunks
    resp = _stream_request([frame[:3].hex(), frame[3:].hex()])
    assert resp.status_code == 200
    assert resp.json()["frames"][0]["hex"] == frame.hex()


def _register_stream_marker():
    client.post("/api/v1/definitions", json={
        "device_type": "stream_marker",
        "version": "1.0.0",
        "fields": [
            {"name": "s1", "type": "uint", "bit_length": 8},
            {"name": "s2", "type": "uint", "bit_length": 8},
            {"name": "length", "type": "uint", "bit_length": 8},
            {"name": "a", "type": "uint", "bit_length": 8},
            {"name": "b", "type": "uint", "bit_length": 8},
            {"name": "c", "type": "uint", "bit_length": 8},
        ],
    })


def test_stream_sync_marker_split_across_chunks():
    # Two-byte sync "AABB" straddles chunks: ...AA | BB...
    # The definition covers the whole framed message (sync + length + body):
    # aabb sync, 1-byte length=3 counting post-length bytes, then 3 bytes.
    _register_stream_marker()
    payload = {
        "device_type": "stream_marker",
        "version": "1.0.0",
        "sync_hex": "aabb",
        "length_field_offset": 2,
        "length_field_bytes": 1,
        "length_endianness": "big",
        "length_adjustment": 0,
        "chunks": ["77aa", "bb03112233"],
    }
    resp = client.post("/api/v1/stream/parse", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # header 3 bytes (aabb + len=3) + 3 body bytes = 6 bytes, at offset 1
    assert data["frame_count"] == 1
    fr = data["frames"][0]
    assert fr["start"] == 1  # leading 77 discarded
    assert fr["hex"] == "aabb03112233"
    assert data["discarded_ranges"][0]["hex"] == "77"
    fields = {f["name"]: f for f in fr["result"]["fields"]}
    assert fields["a"]["value"] == 0x11
    assert fields["c"]["value"] == 0x33


def test_stream_trailing_partial_frame_is_incomplete_not_error():
    frame = _stream_frame(3, 10)
    resp = _stream_request([frame[:5].hex()])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frame_count"] == 0
    assert data["frames"] == []
    assert data["incomplete_hex"] == frame[:5].hex()
    assert data["incomplete_bytes"] == 5

    # noise before the partial frame is still discarded
    resp = _stream_request(["ff" + frame[:5].hex()])
    data = resp.json()
    assert data["frame_count"] == 0
    assert data["incomplete_hex"] == frame[:5].hex()
    assert data["discarded_ranges"][0] == {
        "start": 0, "end": 1, "length": 1, "hex": "ff",
        "reason": REASON_NOISE, "detail": None,
    }

    # partial sync marker alone at the tail is retained too
    resp = _stream_request(["ffaa"])
    data = resp.json()
    assert data["incomplete_hex"] == "aa"


def test_stream_corrupt_checksum_resyncs_one_byte():
    good = _stream_frame(1, 50)
    bad = bytearray(_stream_frame(2, 60))
    bad[-1] ^= 0xFF  # flip checksum byte
    stream = bytes(bad) + good
    resp = _stream_request([stream.hex()])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frame_count"] == 1
    fr = data["frames"][0]
    assert fr["start"] == 6
    fields = {f["name"]: f for f in fr["result"]["fields"]}
    assert fields["seq"]["value"] == 1
    # all 6 bytes of the corrupt frame were rejected byte by byte; the first
    # record is the bad sync byte flagged with the checksum reason
    discarded = data["discarded_ranges"]
    assert sum(d["end"] - d["start"] for d in discarded) == 6
    assert discarded[0]["start"] == 0
    assert discarded[0]["reason"] == REASON_CHECKSUM
    assert "checksum mismatch" in discarded[0]["detail"]
    assert data["incomplete_hex"] == ""


def test_stream_illegal_length_resyncs():
    # A negative adjustment lets a zero length yield a frame shorter than its
    # own header: frame_len = 2 + 0 - 1 = 1 < 2 header bytes. A valid frame
    # under the same spec carries length byte 5 (2 + 5 - 1 = 6 total), so
    # build one by hand with the adjusted length byte.
    body = bytes([0xAA, 5, 9]) + (1).to_bytes(2, "big", signed=True)
    good = body + bytes([xor8(body)])
    bad = bytes.fromhex("aa0011")
    resp = _stream_request(
        [(bad + good).hex()], length_adjustment=-1
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frame_count"] == 1
    assert data["frames"][0]["start"] == 3
    first = data["discarded_ranges"][0]
    assert (first["start"], first["end"]) == (0, 1)
    assert first["reason"] == REASON_INVALID_LENGTH
    assert "illegal frame length" in first["detail"]
    # after dropping the bad AA, "0011" is noise up to the next sync
    assert [(d["start"], d["end"]) for d in data["discarded_ranges"][1:]] == [(1, 3)]


def test_stream_zero_payload_frame_is_rejected_by_parser():
    # length=0 with no adjustment declares a 2-byte frame: the framer accepts
    # the length, but the definition needs 7 bytes -> parse rejection resync.
    bad = bytes.fromhex("aa00")
    good = _stream_frame(8, 2)
    resp = _stream_request([(bad + good).hex()])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frame_count"] == 1
    assert data["frames"][0]["start"] == 2
    first = data["discarded_ranges"][0]
    assert (first["start"], first["end"]) == (0, 1)
    assert first["reason"] == REASON_PARSE_ERROR
    assert "too short" in first["detail"]


def test_stream_parse_failure_resyncs_on_next_sync():
    # length=2 declares a 4-byte frame but stream_dev fields need 7 bytes:
    # the candidate arrives complete (frame_len bytes present), the parser
    # fails with "message too short", so the sync byte is rejected and the
    # framer resyncs to the following valid frame.
    bogus = bytes([0xAA, 2, 0x01, 0x00])  # 4 bytes, too short for 7-byte def
    good = _stream_frame(4, -40)
    resp = _stream_request([(bogus + good).hex()])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frame_count"] == 1
    assert data["frames"][0]["start"] == 4
    first = data["discarded_ranges"][0]
    assert (first["start"], first["end"]) == (0, 1)
    assert first["reason"] == REASON_PARSE_ERROR
    assert "too short" in first["detail"]
    # bytes 1..3 are noise between the rejected sync and the next one
    assert [(d["start"], d["end"], d["reason"]) for d in data["discarded_ranges"][1:]] == [
        (1, 4, REASON_NOISE),
    ]

    # a short candidate still in transit is not rejected: it stays buffered
    waiting = bytes([0xAA, 4, 1, 0x00, 0x00])  # 5 bytes of a 6-byte frame
    resp = _stream_request([waiting.hex()])
    assert resp.status_code == 200
    data = resp.json()
    assert data["frame_count"] == 0
    assert data["incomplete_hex"] == waiting.hex()


def test_stream_trailing_bits_frame_rejected():
    # A definition whose fields end before the declared frame length leaves
    # trailing bits: the stream_marker device (sync 2 + length 1 + body 3 =
    # 6 bytes), so declaring length=4 (a 7-byte frame) leaves 8 trailing
    # bits -> the candidate is rejected and resync proceeds one byte at a
    # time. A trailing byte follows so it cannot stay "incomplete".
    _register_stream_marker()
    payload = {
        "device_type": "stream_marker",
        "version": "1.0.0",
        "sync_hex": "aabb",
        "length_field_offset": 2,
        "length_field_bytes": 1,
        "length_endianness": "big",
        "length_adjustment": 0,
        "chunks": ["aabb0411223344ff"],  # 7-byte candidate + trailing noise
    }
    resp = client.post("/api/v1/stream/parse", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frame_count"] == 0
    first = data["discarded_ranges"][0]
    assert (first["start"], first["end"]) == (0, 1)  # sync byte rejected
    assert "trailing bit" in first["detail"]


def test_stream_length_endianness_and_adjustment():
    # little-endian 2-byte length field counting the *whole* frame:
    # sync=AA, length at [1,3), total = raw + adjustment(0... need raw==total)
    # frame_len = 3 + raw + adj; to make raw==total use adj=-3.
    device = "stream_le"
    assert client.post("/api/v1/definitions", json={
        "device_type": device,
        "version": "1.0.0",
        "fields": [
            {"name": "sync", "type": "uint", "bit_length": 8},
            {"name": "length", "type": "uint", "bit_length": 16, "endianness": "little"},
            {"name": "v", "type": "uint", "bit_length": 16},
        ],
    }).status_code == 201
    body = bytes([0xAA]) + (5).to_bytes(2, "little") + (0x1234).to_bytes(2, "big")
    resp = client.post("/api/v1/stream/parse", json={
        "device_type": device,
        "version": "1.0.0",
        "sync_hex": "aa",
        "length_field_offset": 1,
        "length_field_bytes": 2,
        "length_endianness": "little",
        "length_adjustment": -3,
        "chunks": ["00", body.hex()],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frame_count"] == 1
    assert (data["frames"][0]["start"], data["frames"][0]["end"]) == (1, 6)
    fields = {f["name"]: f for f in data["frames"][0]["result"]["fields"]}
    assert fields["v"]["value"] == 0x1234
    assert data["discarded_ranges"][0]["hex"] == "00"


def test_stream_noise_repeating_sync_start_bytes():
    # multi-byte marker AABB: stray AA bytes that never get their BB are
    # noise; a partial AA at the very tail is retained as incomplete.
    _register_stream_marker()
    resp = client.post("/api/v1/stream/parse", json={
        "device_type": "stream_marker",
        "version": "1.0.0",
        "sync_hex": "aabb",
        "length_field_offset": 2,
        "length_field_bytes": 1,
        "length_endianness": "big",
        "length_adjustment": 0,
        # AA AA (both stray) | AABB 03 112233 | trailing AA (partial marker)
        "chunks": ["aaaaaabb03112233aa"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frame_count"] == 1
    assert data["frames"][0]["hex"] == "aabb03112233"
    assert data["frames"][0]["start"] == 2
    # the two stray AAs before the marker are noise; trailing AA incomplete
    assert [(d["start"], d["end"], d["hex"]) for d in data["discarded_ranges"]] == [
        (0, 2, "aaaa"),
    ]
    assert data["incomplete_hex"] == "aa"


def test_stream_length_endianness_defaults_to_big():
    # omitting length_endianness must parse a big-endian length
    frame = _stream_frame(6, 300)
    resp = client.post("/api/v1/stream/parse", json={
        "device_type": "stream_dev",
        "version": "1.0.0",
        "sync_hex": "aa",
        "length_field_offset": 1,
        "length_field_bytes": 1,
        "chunks": [frame.hex()],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["frame_count"] == 1


def test_stream_defaults_to_latest_active_version():
    # register a byte-compatible 2.0.0 (same wire layout); without version
    # the latest active one must be used.
    v2 = dict(STREAM_DEF, version="2.0.0", description="latest")
    assert client.post("/api/v1/definitions", json=v2).status_code == 201
    frame = _stream_frame(1, 25)
    resp = client.post("/api/v1/stream/parse", json={
        "device_type": "stream_dev",
        **STREAM_PARAMS,
        "chunks": [frame.hex()],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == "2.0.0"

    # explicit version still selects the older one
    resp = _stream_request([frame.hex()])
    assert resp.json()["version"] == "1.0.0"


def test_stream_empty_chunks_is_valid_empty_stream():
    resp = _stream_request([])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["frames"] == []
    assert data["discarded_ranges"] == []
    assert data["incomplete_hex"] == ""
    assert data["received_bytes"] == 0
    assert data["limits"] == {"max_buffer_bytes": 1024 * 1024, "max_frame_bytes": 64 * 1024}


def test_stream_hex_cleaning_matches_parse():
    frame = _stream_frame(5, 7)
    messy = "0x" + "_".join(
        frame.hex()[i:i + 2] for i in range(0, len(frame.hex()), 2)
    )
    resp = _stream_request([messy])
    assert resp.status_code == 200, resp.text
    assert resp.json()["frame_count"] == 1


def test_stream_invalid_chunk_hex_422():
    resp = _stream_request(["zz"])
    assert resp.status_code == 422
    assert "chunks[0]" in resp.json()["detail"]

    resp = _stream_request(["aa", ""])
    assert resp.status_code == 422
    assert "chunks[1]" in resp.json()["detail"]

    resp = _stream_request(["abcd"], sync_hex="zz")
    assert resp.status_code == 422


def test_stream_unknown_device_404():
    resp = client.post("/api/v1/stream/parse", json={
        "device_type": "ghost", **STREAM_PARAMS, "chunks": ["aa"],
    })
    assert resp.status_code == 404


def test_stream_request_validation_422():
    base = {"device_type": "stream_dev", **STREAM_PARAMS, "chunks": []}

    def post(**kw):
        body = dict(base)
        body.update(kw)
        return client.post("/api/v1/stream/parse", json=body)

    assert post(length_field_bytes=0).status_code == 422
    assert post(length_field_bytes=9).status_code == 422
    assert post(length_field_offset=-1).status_code == 422
    assert post(length_endianness="middle").status_code == 422
    assert post(length_adjustment=2 ** 32).status_code == 422
    # unknown body fields are rejected
    assert post(extra=1).status_code == 422
    # length field beyond the 64 KiB frame cap
    assert post(length_field_offset=64 * 1024, length_field_bytes=1).status_code == 422


def test_stream_declared_frame_over_64kib_422():
    # 2-byte big-endian length with adjustment so frame_len = 3 + 65534 - 1
    # = 65536 exactly is allowed (waits for bytes); one more triggers 422.
    header_ok = bytes([0xAA]) + (65534).to_bytes(2, "big")
    header_big = bytes([0xAA]) + (65535).to_bytes(2, "big")
    common = {
        "device_type": "stream_dev",
        "sync_hex": "aa",
        "length_field_offset": 1,
        "length_field_bytes": 2,
        "length_endianness": "big",
        "length_adjustment": -1,  # frame_len = 3 + raw - 1
    }
    resp = client.post("/api/v1/stream/parse", json={**common, "chunks": [header_ok.hex()]})
    assert resp.status_code == 200
    assert resp.json()["incomplete_bytes"] == 3  # held waiting for the body

    resp = client.post("/api/v1/stream/parse", json={**common, "chunks": [header_big.hex()]})
    assert resp.status_code == 422
    assert "65536" in resp.json()["detail"]


# ------------------------------------------------- framer unit-level behavior --


def _echo_framer(**spec_kw):
    return StreamFramer(
        FrameSpec(sync=b"\xAA", length_field_offset=1, length_field_bytes=1,
                  length_endianness="big", length_adjustment=0, **spec_kw),
        validate=lambda data: data,
    )


def test_framer_buffer_limit_raises_framing_error():
    # offset beyond the buffer limit: the header can never complete, so the
    # retained sync + zeros accumulate until the 1 MiB cap.
    framer = StreamFramer(
        FrameSpec(sync=b"\xAB", length_field_offset=MAX_BUFFER_SIZE + 16,
                  length_field_bytes=1, length_endianness="big",
                  length_adjustment=0),
        validate=lambda data: data,
    )
    try:
        framer.feed(b"\xAB" + b"\x00" * MAX_BUFFER_SIZE)
    except FramingError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("buffer limit must raise FramingError")


def test_framer_callback_reject_reason_is_preserved():
    def validate(data):
        raise RejectFrame("nope", reason=REASON_CHECKSUM)

    framer = StreamFramer(
        FrameSpec(sync=b"\xAA", length_field_offset=1, length_field_bytes=1,
                  length_endianness="big", length_adjustment=0),
        validate=validate,
    )
    framer.feed(bytes.fromhex("aa01") + b"x")
    assert framer.frames == []
    assert framer.discarded[0].reason == REASON_CHECKSUM
    assert framer.discarded[0].detail == "nope"
    # one-byte resync: only the sync byte was attributed to the rejection
    assert (framer.discarded[0].start, framer.discarded[0].end) == (0, 1)

