"""GSDML parsing against a realistic, namespaced GSDML V2.35 file."""

from __future__ import annotations

import os
import shutil

import pytest
from helpers import SAMPLE_GSDML

from profinet_mcp.gsd_parser import GSDML_NAMESPACE, GSDMLError, GSDParser


@pytest.fixture
def entry():
    return GSDParser().load(SAMPLE_GSDML)


def test_metadata_comes_from_device_identity(entry) -> None:
    assert entry.metadata == {
        "device_id": "0x0001",
        "vendor_id": "0x0F00",
        "vendor": "IndustriAgents Test Fixtures",
        "name": "IA Sample IO-Device",
        "info_text": "Sample PROFINET IO device with digital and analog modules",
        "main_family": "I/O",
        "product_family": "IA Sample IO",
        "version": "V2.35",
        "profile_revision": "1.00",
        "namespace": GSDML_NAMESPACE,
    }


def test_modules_are_module_items_with_resolved_names(entry) -> None:
    by_id = {module["id"]: module for module in entry.modules}
    assert list(by_id) == ["IDM_DI8", "IDM_DO8", "IDM_AI2", "IDM_AIO"]

    di8 = by_id["IDM_DI8"]
    assert di8["module_ident_number"] == "0x00000010"
    assert di8["name"] == "DI 8x24VDC"  # primary language, not the German text
    assert di8["order_number"] == "IA-DI8"
    (sub,) = di8["submodules"]
    assert (sub["id"], sub["submodule_ident_number"]) == ("IDSM_DI8", "0x00000011")
    assert (sub["input_bytes"], sub["output_bytes"]) == (1, 0)
    assert sub["inputs"] == [{"data_type": "Unsigned8", "name": "Digital inputs", "bytes": 1}]

    assert by_id["IDM_DO8"]["submodules"][0]["output_bytes"] == 1
    assert by_id["IDM_AI2"]["submodules"][0]["input_bytes"] == 4  # 2 x Integer16
    aio = by_id["IDM_AIO"]["submodules"][0]
    assert (aio["input_bytes"], aio["output_bytes"]) == (5, 4)  # Float32+Status8, OctetString Length=4


def test_device_access_point(entry) -> None:
    (dap,) = entry.device_access_points
    assert dap["id"] == "DAP_1"
    assert dap["name"] == "IA Sample IO-Device"
    assert dap["order_number"] == "IA-SIO-100"
    assert dap["dns_compatible_name"] == "ia-sample-io"
    assert dap["physical_slots"] == "0..4"
    assert dap["software_release"] == "V1.2.0"
    assert (dap["max_input_length"], dap["max_output_length"]) == (256, 256)
    assert [m["module_id"] for m in dap["useable_modules"]] == ["IDM_DI8", "IDM_DO8", "IDM_AI2", "IDM_AIO"]
    assert dap["useable_modules"][3]["fixed_in_slots"] == "4"
    assert [(s["type"], s["subslot_number"], s["name"]) for s in dap["system_submodules"]] == [
        ("interface", 32768, "Interface"),
        ("port", 32769, "Port 1"),
        ("port", 32770, "Port 2"),
    ]


def strip_namespace(text: str) -> str:
    return text.replace(f' xmlns="{GSDML_NAMESPACE}"', "")


def test_unnamespaced_file_still_parses(tmp_path) -> None:
    path = tmp_path / "plain.xml"
    path.write_bytes(strip_namespace(SAMPLE_GSDML.read_bytes().decode("latin-1")).encode("latin-1"))
    entry = GSDParser().load(path)
    assert len(entry.modules) == 4
    assert entry.metadata["vendor_id"] == "0x0F00"
    assert entry.metadata["namespace"] is None


def test_unknown_data_type_size_is_reported_as_unknown(tmp_path) -> None:
    text = SAMPLE_GSDML.read_bytes().decode("latin-1")
    text = text.replace('DataType="Integer16" TextId="TOK_AI_Ch1"', 'DataType="61131_STRING" TextId="TOK_AI_Ch1"')
    path = tmp_path / "GSDML-V2.35-x.xml"
    path.write_bytes(text.encode("latin-1"))
    sub = {m["id"]: m for m in GSDParser().load(path).modules}["IDM_AI2"]["submodules"][0]
    assert sub["input_bytes"] is None  # not guessed
    assert [item["bytes"] for item in sub["inputs"]] == [2, None]


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        (b"PROFIBUS GSD text, not XML", "not well-formed XML"),
        (b'<?xml version="1.0"?><root xmlns="urn:other"/>', "not a GSDML file"),
        (b'<?xml version="1.0"?><ISO15745Profile xmlns="urn:other"/>', "not a GSDML file"),
        (f'<?xml version="1.0"?><ISO15745Profile xmlns="{GSDML_NAMESPACE}"/>'.encode(), "no ProfileBody"),
    ],
)
def test_non_gsdml_files_are_rejected(tmp_path, content: bytes, fragment: str) -> None:
    path = tmp_path / "bad.xml"
    path.write_bytes(content)
    with pytest.raises(GSDMLError, match=fragment):
        GSDParser().load(path)


def test_external_entities_are_not_resolved(tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    text = SAMPLE_GSDML.read_bytes().decode("latin-1")
    text = text.replace(
        "<ISO15745Profile ", f'<!DOCTYPE ISO15745Profile [<!ENTITY x SYSTEM "file://{secret}">]>\n<ISO15745Profile ', 1
    )
    text = text.replace(
        'Value="IndustriAgents Test Fixtures"/>\n    </DeviceIdentity>', 'Value="&x;"/>\n    </DeviceIdentity>'
    )
    path = tmp_path / "xxe.xml"
    path.write_bytes(text.encode("latin-1"))
    try:
        entry = GSDParser().load(path)
    except GSDMLError:
        return  # refusing the file is fine too
    assert "TOP-SECRET" not in repr(entry.metadata)


def test_cache_notices_edits(tmp_path) -> None:
    """Regression: load_cached was functools.lru_cache on a method, so edits were never re-read."""
    path = tmp_path / SAMPLE_GSDML.name
    shutil.copy(SAMPLE_GSDML, path)
    parser = GSDParser()
    first, cached = parser.load_cached(path)
    assert cached is False
    again, cached = parser.load_cached(str(path))
    assert cached is True and again is first

    text = path.read_bytes().decode("latin-1").replace('DeviceID="0x0001"', 'DeviceID="0x0002"')
    path.write_bytes(text.encode("latin-1"))
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    edited, cached = parser.load_cached(path)
    assert cached is False
    assert edited.metadata["device_id"] == "0x0002"


def test_base_path_lookup(tmp_path) -> None:
    shutil.copy(SAMPLE_GSDML, tmp_path / SAMPLE_GSDML.name)
    entry, _ = GSDParser(tmp_path).load_cached(SAMPLE_GSDML.name)
    assert entry.path == (tmp_path / SAMPLE_GSDML.name).resolve()
    with pytest.raises(FileNotFoundError):
        GSDParser(tmp_path).load_cached("GSDML-missing.xml")
