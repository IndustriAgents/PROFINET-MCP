"""GSDML parsing against a realistic, namespaced GSDML V2.35 file."""

from __future__ import annotations

import http.server
import os
import shutil
import threading
import time

import pytest
from helpers import SAMPLE_GSDML
from lxml import etree

from profinet_mcp import gsd_parser
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
    assert list(by_id) == ["IDM_DI8", "IDM_DO8", "IDM_AI2", "IDM_AIO", "IDM_CARRIER"]

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


def test_pluggable_submodules_are_resolved(entry) -> None:
    """UseableSubmodules/SubmoduleItemRef point into ApplicationProcess/SubmoduleList."""
    carrier = {module["id"]: module for module in entry.modules}["IDM_CARRIER"]
    assert carrier["submodules"] == []  # no fixed (virtual) submodules
    assert carrier["physical_subslots"] == "1..2"
    assert carrier["submodules_incomplete"] is False
    di16, do16 = carrier["pluggable_submodules"]
    assert (di16["id"], di16["name"], di16["order_number"]) == ("IDSM_PLUG_DI16", "DI 16x24VDC submodule", "IA-PDI16")
    assert (di16["input_bytes"], di16["output_bytes"], di16["allowed_in_subslots"]) == (2, 0, "1..2")
    assert (do16["input_bytes"], do16["output_bytes"], do16["fixed_in_subslots"]) == (0, 2, "2")
    assert [sub["id"] for sub in entry.submodules] == ["IDSM_PLUG_DI16", "IDSM_PLUG_DO16"]
    others = [m for m in entry.modules if m["id"] != "IDM_CARRIER"]
    assert all(m["pluggable_submodules"] == [] and m["submodules_incomplete"] is False for m in others)


def test_unresolved_submodule_reference_is_flagged(tmp_path) -> None:
    text = SAMPLE_GSDML.read_bytes().decode("latin-1")
    text = text.replace('SubmoduleItemTarget="IDSM_PLUG_DO16"', 'SubmoduleItemTarget="IDSM_MISSING"')
    path = tmp_path / "GSDML-V2.35-missing-ref.xml"
    path.write_bytes(text.encode("latin-1"))
    carrier = {m["id"]: m for m in GSDParser().load(path).modules}["IDM_CARRIER"]
    assert carrier["submodules_incomplete"] is True
    assert carrier["pluggable_submodules"][1] == {
        "id": "IDSM_MISSING",
        "unresolved": True,
        "allowed_in_subslots": "1..2",
        "fixed_in_subslots": "2",
        "used_in_subslots": None,
    }


def test_device_access_point(entry) -> None:
    (dap,) = entry.device_access_points
    assert dap["id"] == "DAP_1"
    assert dap["name"] == "IA Sample IO-Device"
    assert dap["order_number"] == "IA-SIO-100"
    assert dap["dns_compatible_name"] == "ia-sample-io"
    assert dap["physical_slots"] == "0..4"
    assert dap["software_release"] == "V1.2.0"
    assert (dap["max_input_length"], dap["max_output_length"]) == (256, 256)
    assert [m["module_id"] for m in dap["useable_modules"]] == [
        "IDM_DI8",
        "IDM_DO8",
        "IDM_AI2",
        "IDM_AIO",
        "IDM_CARRIER",
    ]
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
    assert len(entry.modules) == 5
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


# -- hostile XML ---------------------------------------------------------------------------
#
# Entities go into element text (<ProfileRevision>). An external entity inside an *attribute* value is a
# well-formedness error for every parser, so a test built that way could never fail.

SECRET = "TOP-SECRET-7f3a91"


def with_doctype(doctype: str, revision: str) -> bytes:
    text = SAMPLE_GSDML.read_bytes().decode("latin-1")
    text = text.replace("<ISO15745Profile ", doctype + "\n<ISO15745Profile ", 1)
    text = text.replace("<ProfileRevision>1.00</ProfileRevision>", f"<ProfileRevision>{revision}</ProfileRevision>", 1)
    return text.encode("latin-1")


def unhardened_parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=True, no_network=False, load_dtd=True, huge_tree=True)


@pytest.fixture
def loopback_http():
    """A local HTTP server that records every fetch (to prove nothing is downloaded)."""
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            hits.append(self.path)
            body = b'<!ENTITY x "FETCHED-OVER-HTTP">' if self.path.endswith(".dtd") else b"FETCHED-OVER-HTTP"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", hits
    finally:
        server.shutdown()
        server.server_close()


def secret_file(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text(SECRET)
    return secret


def assert_entity_not_expanded(tmp_path, doctype: str) -> None:
    path = tmp_path / "xxe.xml"
    path.write_bytes(with_doctype(doctype, "&x;"))
    entry = GSDParser().load(path)  # must parse: refusing is not what is being tested here
    assert SECRET not in repr(entry)
    assert entry.metadata["profile_revision"] is None
    assert len(entry.modules) == 5  # the rest of the file is still read


def file_entity(tmp_path) -> str:
    """Internal subset declaring an external entity that points at a local secret."""
    return f'<!DOCTYPE ISO15745Profile [<!ENTITY x SYSTEM "file://{secret_file(tmp_path)}">]>'


def external_dtd(tmp_path) -> str:
    """External DTD (a local file) whose entity carries the secret."""
    dtd = tmp_path / "ext.dtd"
    dtd.write_text(f'<!ENTITY x "{SECRET}">')
    return f'<!DOCTYPE ISO15745Profile SYSTEM "file://{dtd}">'


@pytest.mark.parametrize("make_doctype", [file_entity, external_dtd], ids=["file-entity", "external-dtd-file"])
def test_entities_are_not_expanded(tmp_path, make_doctype) -> None:
    assert_entity_not_expanded(tmp_path, make_doctype(tmp_path))


@pytest.mark.parametrize("make_doctype", [file_entity, external_dtd], ids=["file-entity", "external-dtd-file"])
def test_xxe_checks_fail_against_an_unhardened_parser(tmp_path, monkeypatch, make_doctype) -> None:
    """Control: with a permissive parser the secret leaks, so the checks above are able to fail."""
    monkeypatch.setattr(gsd_parser, "_xml_parser", unhardened_parser)
    with pytest.raises(AssertionError):
        assert_entity_not_expanded(tmp_path, make_doctype(tmp_path))


LAUGHS = (
    '<!DOCTYPE ISO15745Profile [<!ENTITY a "aaaaaaaaaa">'
    + "".join(f'<!ENTITY {chr(98 + i)} "' + f"&{chr(97 + i)};" * 10 + '">' for i in range(9))
    + "]>"
)


@pytest.mark.parametrize(
    ("doctype", "revision"),
    [
        (LAUGHS, "&j;"),  # billion laughs: 10^10 bytes if expanded
        ('<!DOCTYPE ISO15745Profile [<!ENTITY x "' + "A" * 100_000 + '">]>', "&x;" * 2000),  # quadratic blowup
    ],
    ids=["billion-laughs", "quadratic"],
)
def test_entity_expansion_bombs_are_harmless(tmp_path, doctype: str, revision: str) -> None:
    path = tmp_path / "bomb.xml"
    path.write_bytes(with_doctype(doctype, revision))
    started = time.perf_counter()
    try:
        entry = GSDParser().load(path)
    except GSDMLError as exc:
        assert "entity" in str(exc).lower()  # refused by libxml2's amplification limit
    else:
        assert entry.metadata["profile_revision"] is None  # not expanded
    assert time.perf_counter() - started < 5


def network_cases(base: str) -> dict[str, tuple[str, str]]:
    return {
        "external-dtd": (f'<!DOCTYPE ISO15745Profile SYSTEM "{base}/ext.dtd">', "&x;"),
        "parameter-entity": (f'<!DOCTYPE ISO15745Profile [<!ENTITY % p SYSTEM "{base}/p.dtd"> %p;]>', "&x;"),
        "general-entity": (f'<!DOCTYPE ISO15745Profile [<!ENTITY x SYSTEM "{base}/e.txt">]>', "&x;"),
    }


def parse_network_cases(tmp_path, base: str) -> list[str]:
    seen = []
    for name, (doctype, revision) in network_cases(base).items():
        path = tmp_path / f"{name}.xml"
        path.write_bytes(with_doctype(doctype, revision))
        try:
            entry = GSDParser().load(path)
        except GSDMLError:
            continue
        seen.append(repr(entry))
    return seen


def test_no_network_access_while_parsing(tmp_path, loopback_http) -> None:
    # The libxml2 bundled with lxml 6 has no HTTP client; no_network=True keeps a build that has one
    # from fetching. The control below runs only where fetching is possible at all.
    base, hits = loopback_http
    parsed = parse_network_cases(tmp_path, base)
    assert hits == []
    assert not any("FETCHED-OVER-HTTP" in text for text in parsed)


@pytest.mark.skipif(
    "http" not in getattr(etree, "LIBXML_FEATURES", ()),
    reason="this libxml2 build has no HTTP client, so no parser setting can fetch over the network",
)
def test_network_check_fails_against_an_unhardened_parser(tmp_path, loopback_http, monkeypatch) -> None:
    """Control: an unhardened parser does fetch from the listener, so the check above can fail."""
    monkeypatch.setattr(gsd_parser, "_xml_parser", unhardened_parser)
    base, hits = loopback_http
    parse_network_cases(tmp_path, base)
    assert hits, "the unhardened control parser should have fetched something"


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
