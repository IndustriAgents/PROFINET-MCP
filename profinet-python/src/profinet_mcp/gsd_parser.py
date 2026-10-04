"""GSDML (GSD Markup Language) parser for PROFINET device description files.

Real GSDML files put every element in the namespace
``http://www.profibus.com/GSDML/2003/11/DeviceProfile`` and keep the device
identity in ``ProfileBody/DeviceIdentity``, the device access points in
``DeviceAccessPointList`` and the pluggable modules in ``ModuleList/ModuleItem``.
Display names are ``TextId`` references into ``ExternalTextList/PrimaryLanguage``.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

GSDML_NAMESPACE = "http://www.profibus.com/GSDML/2003/11/DeviceProfile"
XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
MAX_FILE_BYTES = 64 * 1024 * 1024
CACHE_SIZE = 64

#: Byte sizes of GSDML ``DataItem`` data types. Types not listed here (or
#: string types without a ``Length``) are reported with an unknown size.
DATA_TYPE_BYTES: dict[str, int] = {
    "Integer8": 1,
    "Integer16": 2,
    "Integer32": 4,
    "Integer64": 8,
    "Unsigned8": 1,
    "Unsigned16": 2,
    "Unsigned32": 4,
    "Unsigned64": 8,
    "Float32": 4,
    "Float64": 8,
    "Unsigned8+Unsigned8": 2,
    "Float32+Status8": 5,
    "Float32+Unsigned8": 5,
    "Unsigned8_S": 1,
    "Unsigned16_S": 2,
    "Integer16_S": 2,
    "N2": 2,
    "N4": 4,
    "V2": 2,
    "E2": 2,
    "C4": 4,
    "X2": 2,
    "X4": 4,
    "Unipolar2.16": 2,
    "Date": 7,
    "TimeOfDay with date indication": 6,
    "TimeOfDay without date indication": 4,
    "TimeDifference with date indication": 6,
    "TimeDifference without date indication": 4,
    "NetworkTime": 8,
    "NetworkTimeDifference": 8,
    "F_MessageTrailer4Byte": 4,
    "F_MessageTrailer5Byte": 5,
}
LENGTH_TYPES = frozenset({"OctetString", "VisibleString", "OctetString_S", "UnicodeString8"})

_SCHEMA_VERSION_RE = re.compile(r"GSDML-DeviceProfile-(V\d+\.\d+)\.xsd", re.IGNORECASE)
_FILENAME_VERSION_RE = re.compile(r"^GSDML-(V\d+\.\d+)-", re.IGNORECASE)


class GSDMLError(ValueError):
    """The file exists but is not a usable GSDML file."""


@dataclass(slots=True)
class GSDCacheEntry:
    path: Path
    modules: list[dict[str, Any]]
    metadata: dict[str, Any]
    device_access_points: list[dict[str, Any]] = field(default_factory=list)


class _Doc:
    """Namespace-aware helpers around one parsed document."""

    def __init__(self, root: etree._Element) -> None:
        self.root = root
        self.ns = etree.QName(root).namespace
        self.texts: dict[str, str] = {}
        primary = self.find(root, "ProfileBody", "ApplicationProcess", "ExternalTextList", "PrimaryLanguage")
        if primary is not None:
            for text in self.children(primary, "Text"):
                text_id = text.get("TextId")
                if text_id is not None and text.get("Value") is not None:
                    self.texts[text_id] = text.get("Value")

    def tag(self, name: str) -> str:
        return f"{{{self.ns}}}{name}" if self.ns else name

    def find(self, element: etree._Element | None, *path: str) -> etree._Element | None:
        for name in path:
            if element is None:
                return None
            element = element.find(self.tag(name))
        return element

    def children(self, element: etree._Element | None, name: str) -> list[etree._Element]:
        return [] if element is None else element.findall(self.tag(name))

    def text_ref(self, element: etree._Element | None) -> str | None:
        """Resolve an element's ``TextId`` through the primary language text list."""
        if element is None:
            return None
        text_id = element.get("TextId")
        return self.texts.get(text_id) if text_id else None

    def value(self, element: etree._Element | None, *path: str) -> str | None:
        found = self.find(element, *path)
        return found.get("Value") if found is not None else None


class GSDParser:
    """Loads GSDML files, with a small cache that notices when a file changes."""

    def __init__(self, base_path: Path | str | None = None) -> None:
        self.base_path = Path(base_path).expanduser() if base_path else None
        self._cache: OrderedDict[Path, tuple[int, int, GSDCacheEntry]] = OrderedDict()
        self._lock = threading.Lock()

    def resolve(self, filepath: str | Path) -> Path:
        path = Path(filepath).expanduser()
        if path.is_file():
            return path.resolve()
        if self.base_path:
            for candidate in (self.base_path / path, self.base_path / path.name):
                if candidate.is_file():
                    return candidate.resolve()
        raise FileNotFoundError(f"GSDML file not found: {filepath}")

    def load_cached(self, filepath: str | Path) -> tuple[GSDCacheEntry, bool]:
        """Return ``(entry, from_cache)``; a file is re-parsed when its size or mtime changes."""
        path = self.resolve(filepath)
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._cache.get(path)
            if cached and cached[:2] == key:
                self._cache.move_to_end(path)
                return cached[2], True
        entry = self.load(path)
        with self._lock:
            self._cache[path] = (*key, entry)
            self._cache.move_to_end(path)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return entry, False

    def load(self, filepath: str | Path) -> GSDCacheEntry:
        path = self.resolve(filepath)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise GSDMLError(f"{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB")
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
        try:
            root = etree.parse(str(path), parser).getroot()
        except etree.XMLSyntaxError as exc:
            raise GSDMLError(f"{path.name} is not well-formed XML: {exc}") from exc
        namespace = etree.QName(root).namespace
        if namespace not in (GSDML_NAMESPACE, None) or etree.QName(root).localname != "ISO15745Profile":
            raise GSDMLError(
                f"{path.name} is not a GSDML file (root element {root.tag!r}; expected ISO15745Profile "
                f"in namespace {GSDML_NAMESPACE})"
            )
        doc = _Doc(root)
        body = doc.find(root, "ProfileBody")
        process = doc.find(body, "ApplicationProcess")
        if body is None or process is None:
            raise GSDMLError(f"{path.name} has no ProfileBody/ApplicationProcess")

        daps = [
            self._dap(doc, item)
            for item in doc.children(doc.find(process, "DeviceAccessPointList"), "DeviceAccessPointItem")
        ]
        modules = [self._module(doc, item) for item in doc.children(doc.find(process, "ModuleList"), "ModuleItem")]
        return GSDCacheEntry(
            path=path,
            modules=modules,
            metadata=self._metadata(doc, root, body, daps, path),
            device_access_points=daps,
        )

    # -- sections ---------------------------------------------------------------

    @staticmethod
    def _metadata(
        doc: _Doc, root: etree._Element, body: etree._Element, daps: list[dict[str, Any]], path: Path
    ) -> dict[str, Any]:
        identity = doc.find(body, "DeviceIdentity")
        family = doc.find(body, "DeviceFunction", "Family")
        schema_location = root.get(f"{{{XSI_NAMESPACE}}}schemaLocation") or ""
        match = _SCHEMA_VERSION_RE.search(schema_location) or _FILENAME_VERSION_RE.match(path.name)
        version = match.group(1).upper() if match else None
        if version is None and daps:
            version = daps[0].get("required_schema_version")
        return {
            "device_id": identity.get("DeviceID") if identity is not None else None,
            "vendor_id": identity.get("VendorID") if identity is not None else None,
            "vendor": doc.value(identity, "VendorName"),
            "name": daps[0]["name"] if daps else None,
            "info_text": doc.text_ref(doc.find(identity, "InfoText")),
            "main_family": family.get("MainFamily") if family is not None else None,
            "product_family": family.get("ProductFamily") if family is not None else None,
            "version": version,
            "profile_revision": _element_text(doc.find(root, "ProfileHeader", "ProfileRevision")),
            "namespace": doc.ns,
        }

    @staticmethod
    def _module_info(doc: _Doc, item: etree._Element) -> dict[str, Any]:
        info = doc.find(item, "ModuleInfo")
        return {
            "name": doc.text_ref(doc.find(info, "Name")),
            "info_text": doc.text_ref(doc.find(info, "InfoText")),
            "order_number": doc.value(info, "OrderNumber"),
        }

    def _dap(self, doc: _Doc, item: etree._Element) -> dict[str, Any]:
        info = doc.find(item, "ModuleInfo")
        io_config = doc.find(item, "IOConfigData")
        system = doc.find(item, "SystemDefinedSubmoduleList")
        system_submodules = []
        for kind, tag in (("interface", "InterfaceSubmoduleItem"), ("port", "PortSubmoduleItem")):
            for sub in doc.children(system, tag):
                system_submodules.append(
                    {
                        "type": kind,
                        "id": sub.get("ID"),
                        "subslot_number": _int_or_none(sub.get("SubslotNumber")),
                        "submodule_ident_number": sub.get("SubmoduleIdentNumber"),
                        "name": doc.text_ref(sub),
                    }
                )
        return {
            "id": item.get("ID"),
            "module_ident_number": item.get("ModuleIdentNumber"),
            **self._module_info(doc, item),
            "dns_compatible_name": item.get("DNS_CompatibleName"),
            "physical_slots": item.get("PhysicalSlots"),
            "fixed_in_slots": item.get("FixedInSlots"),
            "required_schema_version": item.get("RequiredSchemaVersion"),
            "hardware_release": doc.value(info, "HardwareRelease"),
            "software_release": doc.value(info, "SoftwareRelease"),
            "max_input_length": _int_or_none(io_config.get("MaxInputLength") if io_config is not None else None),
            "max_output_length": _int_or_none(io_config.get("MaxOutputLength") if io_config is not None else None),
            "useable_modules": [
                {
                    "module_id": ref.get("ModuleItemTarget"),
                    "allowed_in_slots": ref.get("AllowedInSlots"),
                    "fixed_in_slots": ref.get("FixedInSlots"),
                    "used_in_slots": ref.get("UsedInSlots"),
                }
                for ref in doc.children(doc.find(item, "UseableModules"), "ModuleItemRef")
            ],
            "submodules": [self._submodule(doc, sub) for sub in self._virtual_submodules(doc, item)],
            "system_submodules": system_submodules,
        }

    def _module(self, doc: _Doc, item: etree._Element) -> dict[str, Any]:
        return {
            "id": item.get("ID"),
            "module_ident_number": item.get("ModuleIdentNumber"),
            **self._module_info(doc, item),
            "submodules": [self._submodule(doc, sub) for sub in self._virtual_submodules(doc, item)],
        }

    @staticmethod
    def _virtual_submodules(doc: _Doc, item: etree._Element) -> list[etree._Element]:
        return doc.children(doc.find(item, "VirtualSubmoduleList"), "VirtualSubmoduleItem")

    def _submodule(self, doc: _Doc, sub: etree._Element) -> dict[str, Any]:
        io_data = doc.find(sub, "IOData")
        inputs = self._data_items(doc, doc.find(io_data, "Input"))
        outputs = self._data_items(doc, doc.find(io_data, "Output"))
        return {
            "id": sub.get("ID"),
            "submodule_ident_number": sub.get("SubmoduleIdentNumber"),
            "name": doc.text_ref(doc.find(sub, "ModuleInfo", "Name")),
            "input_bytes": _total(inputs),
            "output_bytes": _total(outputs),
            "inputs": inputs,
            "outputs": outputs,
        }

    @staticmethod
    def _data_items(doc: _Doc, direction: etree._Element | None) -> list[dict[str, Any]]:
        items = []
        for item in doc.children(direction, "DataItem"):
            data_type = item.get("DataType")
            size: int | None = DATA_TYPE_BYTES.get(data_type or "")
            if data_type in LENGTH_TYPES:
                size = _int_or_none(item.get("Length"))
            items.append({"data_type": data_type, "name": doc.text_ref(item), "bytes": size})
        return items


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    for base in (10, 0):  # decimal first ("032"), then 0x.. hex
        try:
            return int(value, base)
        except ValueError:
            continue
    return None


def _element_text(element: etree._Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    return element.text.strip() or None


def _total(items: list[dict[str, Any]]) -> int | None:
    sizes = [item["bytes"] for item in items]
    return None if any(size is None for size in sizes) else sum(sizes)
