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
# Layer 1: GSDParser.load refuses any DOCTYPE/ENTITY before parsing (GSDML never needs a DTD).
# Layer 2 (defence in depth): the parser itself does not expand entities, load DTDs or fetch.

SECRET = "TOP-SECRET-7f3a91"
FIXTURE_TEXT = SAMPLE_GSDML.read_bytes().decode("latin-1")


def with_doctype(doctype: str, revision: str = "1.00", vendor: str = "IndustriAgents Test Fixtures") -> bytes:
    """The fixture with a DOCTYPE, entity references in element text (revision) and an attribute (vendor)."""
    text = FIXTURE_TEXT.replace("<ISO15745Profile ", doctype + "\n<ISO15745Profile ", 1)
    text = text.replace("<ProfileRevision>1.00</ProfileRevision>", f"<ProfileRevision>{revision}</ProfileRevision>", 1)
    text = text.replace(
        '<VendorName Value="IndustriAgents Test Fixtures"/>\n    </DeviceIdentity>',
        f'<VendorName Value="{vendor}"/>\n    </DeviceIdentity>',
        1,
    )
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


def secret_file(tmp_path) -> str:
    secret = tmp_path / "secret.txt"
    secret.write_text(SECRET)
    return str(secret)


LAUGHS = (
    '<!DOCTYPE ISO15745Profile [<!ENTITY a "aaaaaaaaaa">'
    + "".join(f'<!ENTITY {chr(98 + i)} "' + f"&{chr(97 + i)};" * 10 + '">' for i in range(9))
    + "]>"
)
QUADRATIC = '<!DOCTYPE ISO15745Profile [<!ENTITY x "' + "A" * 100_000 + '">]>'


def hostile_documents(tmp_path, base: str) -> dict[str, bytes]:
    dtd = tmp_path / "ext.dtd"
    dtd.write_text(f'<!ENTITY x "{SECRET}">')
    file_entity = f'<!DOCTYPE ISO15745Profile [<!ENTITY x SYSTEM "file://{secret_file(tmp_path)}">]>'
    return {
        "file-entity-in-text": with_doctype(file_entity, revision="&x;"),
        "external-dtd-file": with_doctype(f'<!DOCTYPE ISO15745Profile SYSTEM "file://{dtd}">', revision="&x;"),
        "billion-laughs-in-text": with_doctype(LAUGHS, revision="&j;"),
        # At the lxml 4.9.2 floor (libxml2 2.9) entities in attribute values are always expanded: this one
        # took 85 s and 391 MB there before DOCTYPEs were refused up front.
        "quadratic-in-attribute": with_doctype(QUADRATIC, vendor="&x;" * 2000),
        "billion-laughs-in-attribute": with_doctype(LAUGHS, vendor="&j;"),
        "http-external-dtd": with_doctype(f'<!DOCTYPE ISO15745Profile SYSTEM "{base}/ext.dtd">', revision="&x;"),
        "http-parameter-entity": with_doctype(
            f'<!DOCTYPE ISO15745Profile [<!ENTITY % p SYSTEM "{base}/p.dtd"> %p;]>', revision="&x;"
        ),
        "http-general-entity": with_doctype(
            f'<!DOCTYPE ISO15745Profile [<!ENTITY x SYSTEM "{base}/e.txt">]>', revision="&x;"
        ),
        "utf16-doctype": with_doctype(file_entity, revision="&x;")
        .decode("latin-1")
        .replace('encoding="iso-8859-1"', 'encoding="UTF-16"')
        .encode("utf-16"),
    }


def test_hostile_documents_are_refused_before_parsing(tmp_path, loopback_http) -> None:
    base, hits = loopback_http
    for name, content in hostile_documents(tmp_path, base).items():
        path = tmp_path / f"{name}.xml"
        path.write_bytes(content)
        started = time.perf_counter()
        with pytest.raises(GSDMLError, match="contains <!DOCTYPE: GSDML files never need a DTD") as raised:
            GSDParser().load(path)
        assert time.perf_counter() - started < 2, name
        assert SECRET not in str(raised.value), name
    assert hits == []


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        (b'<?xml version="1.0" encoding="UTF-7"?>\n<ISO15745Profile/>', "encoding 'utf-7'"),
        (b'<?xml version="1.0" encoding="EBCDIC-US"?>\n<ISO15745Profile/>', "encoding 'ebcdic-us'"),
        (b'<?xml version="1.0"?>\n<!ENTITY x "y">\n<ISO15745Profile/>', "contains <!ENTITY"),
        (b'<?xml version="1.0"?>\n<!doctype ISO15745Profile>\n<ISO15745Profile/>', "contains <!DOCTYPE"),
    ],
    ids=["utf-7", "ebcdic", "bare-entity", "lowercase-doctype"],
)
def test_encodings_that_could_hide_a_doctype_are_refused(tmp_path, content: bytes, fragment: str) -> None:
    path = tmp_path / "odd.xml"
    path.write_bytes(content)
    with pytest.raises(GSDMLError, match=fragment):
        GSDParser().load(path)


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be", "utf-8-sig"])
def test_unicode_encoded_gsdml_still_parses(tmp_path, codec: str) -> None:
    label = "UTF-8" if codec == "utf-8-sig" else "UTF-16"
    text = FIXTURE_TEXT.replace('encoding="iso-8859-1"', f'encoding="{label}"')
    path = tmp_path / f"GSDML-V2.35-{codec}.xml"
    path.write_bytes(text.encode(codec))
    entry = GSDParser().load(path)
    assert entry.metadata["vendor_id"] == "0x0F00" and len(entry.modules) == 5


def test_files_over_the_size_cap_are_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gsd_parser, "MAX_FILE_BYTES", 1024)
    path = tmp_path / "big.xml"
    path.write_bytes(SAMPLE_GSDML.read_bytes())
    with pytest.raises(GSDMLError, match="larger than"):
        GSDParser().load(path)


# Layer 2: the parser alone, with the pre-check bypassed.


def parse_directly(content: bytes) -> str:
    """Parse with gsd_parser's parser (no DOCTYPE pre-check) and return everything it produced."""
    try:
        root = etree.fromstring(content, gsd_parser._xml_parser())
    except etree.XMLSyntaxError as exc:
        return f"refused: {exc}"
    return etree.tostring(root, encoding="unicode") + "".join(
        f"{element.tag}={value}" for element in root.iter() for value in element.attrib.values()
    )


def text_entity_documents(tmp_path) -> dict[str, bytes]:
    docs = hostile_documents(tmp_path, "http://127.0.0.1:9")
    return {name: docs[name] for name in ("file-entity-in-text", "external-dtd-file", "billion-laughs-in-text")}


@pytest.mark.parametrize("name", ["file-entity-in-text", "external-dtd-file", "billion-laughs-in-text"])
def test_parser_does_not_expand_entities(tmp_path, name: str) -> None:
    output = parse_directly(text_entity_documents(tmp_path)[name])
    assert SECRET not in output
    assert "a" * 10_000 not in output


@pytest.mark.parametrize("name", ["file-entity-in-text", "external-dtd-file"])
def test_parser_checks_fail_against_an_unhardened_parser(tmp_path, monkeypatch, name: str) -> None:
    """Control: with a permissive parser the secret leaks, so the checks above are able to fail."""
    monkeypatch.setattr(gsd_parser, "_xml_parser", unhardened_parser)
    assert SECRET in parse_directly(text_entity_documents(tmp_path)[name])


def parse_network_cases(tmp_path, base: str) -> None:
    docs = hostile_documents(tmp_path, base)
    for name in ("http-external-dtd", "http-parameter-entity", "http-general-entity"):
        parse_directly(docs[name])


def test_parser_does_not_fetch(tmp_path, loopback_http) -> None:
    base, hits = loopback_http
    parse_network_cases(tmp_path, base)
    assert hits == []


def test_network_check_fails_against_an_unhardened_parser(tmp_path, loopback_http, monkeypatch) -> None:
    """Control: an unhardened parser fetches from the listener, so the check above can fail.

    lxml 6 bundles a libxml2 without an HTTP client; there nothing can be fetched at all, and the
    control is skipped rather than passed. Older libxml2 builds (lxml 4.9) do fetch.
    """
    monkeypatch.setattr(gsd_parser, "_xml_parser", unhardened_parser)
    base, hits = loopback_http
    parse_network_cases(tmp_path, base)
    if not hits:
        pytest.skip("this libxml2 build cannot fetch over HTTP even when allowed (no HTTP client)")
    assert any(path.endswith((".dtd", ".txt")) for path in hits)


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
