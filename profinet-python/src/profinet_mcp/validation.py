"""Argument checks shared by the tools. Each raises :class:`InvalidArgument`."""

from __future__ import annotations

import ipaddress
import re

MAX_IO_BYTES = 1440
MAX_SLOT = 0x7FFF
MAX_SUBSLOT = 0xFFFF

_MAC_RE = re.compile(r"^[0-9a-f]{2}([:-])[0-9a-f]{2}(\1[0-9a-f]{2}){4}$")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_PORT_NAME_RE = re.compile(r"^port-\d{3}(-\d{5})?$")
_IP_LIKE_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


class InvalidArgument(ValueError):
    """A tool argument that fails validation; reported as ``success: false``."""


def mac_address(value: str, field: str = "device_mac") -> str:
    """Normalise ``00-11-22-AA-BB-CC`` / ``00:11:22:aa:bb:cc`` to lowercase colon form."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if not _MAC_RE.match(text):
        raise InvalidArgument(f"{field} {value!r} is not a MAC address such as 00:11:22:33:44:55")
    mac = text.replace("-", ":")
    if int(mac[:2], 16) & 1:
        raise InvalidArgument(f"{field} {value!r} is a multicast address, not a device")
    return mac


def station_name(value: str, field: str = "name") -> str:
    """Check a PROFINET NameOfStation against the IEC 61158-6-10 rules.

    Lowercase a-z, 0-9 and '-' in dot-separated labels of 1-63 characters
    (not starting or ending with '-'), at most 240 characters, the first label
    not of the form ``port-xyz`` / ``port-xyz-abcde``, and not ``n.n.n.n``.
    """
    if not isinstance(value, str) or not value:
        raise InvalidArgument(f"{field} must be a non-empty string")
    if len(value) > 240:
        raise InvalidArgument(f"{field} is {len(value)} characters long; the limit is 240")
    if value != value.lower():
        raise InvalidArgument(f"{field} {value!r} must be lowercase (try {value.lower()!r})")
    labels = value.split(".")
    for label in labels:
        if not 1 <= len(label) <= 63:
            raise InvalidArgument(f"{field} {value!r}: each dot-separated label must be 1-63 characters long")
        if not _LABEL_RE.match(label):
            raise InvalidArgument(
                f"{field} {value!r}: labels may only contain a-z, 0-9 and '-', and may not start or end with '-'"
            )
    if _PORT_NAME_RE.match(labels[0]):
        raise InvalidArgument(f"{field} {value!r} must not have the form port-xyz or port-xyz-abcde")
    if _IP_LIKE_RE.match(value):
        raise InvalidArgument(f"{field} {value!r} must not look like an IP address")
    return value


def ipv4(value: str, field: str) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(value.strip() if isinstance(value, str) else value)
    except (ipaddress.AddressValueError, ValueError, TypeError):
        raise InvalidArgument(f"{field} {value!r} is not an IPv4 address") from None


def ip_settings(ip_address: str, subnet_mask: str, gateway: str | None) -> tuple[str, str, str]:
    """Validate an IP suite for DCP Set IP. Returns (ip, mask, gateway); no gateway is ``0.0.0.0``."""
    ip = ipv4(ip_address, "ip_address")
    mask = ipv4(subnet_mask, "subnet_mask")
    bits = int(mask)
    inverted = ~bits & 0xFFFFFFFF
    if bits == 0 or inverted & (inverted + 1):
        raise InvalidArgument(f"subnet_mask {subnet_mask!r} is not a contiguous netmask such as 255.255.255.0")
    network = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
    if ip.is_multicast or ip.is_unspecified or ip.is_loopback or int(ip) == 0xFFFFFFFF:
        raise InvalidArgument(f"ip_address {ip} cannot be assigned to a device")
    if network.prefixlen < 31 and ip in (network.network_address, network.broadcast_address):
        raise InvalidArgument(f"ip_address {ip} is the network or broadcast address of {network}")
    gw = ipv4(gateway, "gateway") if gateway not in (None, "") else ipaddress.IPv4Address("0.0.0.0")
    if int(gw) and gw not in network:
        raise InvalidArgument(f"gateway {gw} is not inside {network}")
    return str(ip), str(mask), str(gw)


def device_identifier(value: str, field: str = "device_name") -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise InvalidArgument(f"{field} must be a non-empty string")
    if len(text) > 240:
        raise InvalidArgument(f"{field} is longer than 240 characters")
    return text


def io_address(slot: object, subslot: object, *, source: str = "") -> tuple[int, int]:
    """Check slot/subslot numbers (used for device-map entries, which skip schema validation)."""
    prefix = f"{source}: " if source else ""
    for name, value, low, high in (("slot", slot, 0, MAX_SLOT), ("subslot", subslot, 1, MAX_SUBSLOT)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidArgument(f"{prefix}{name} must be an integer, got {value!r}")
        if not low <= value <= high:
            raise InvalidArgument(f"{prefix}{name} must be between {low} and {high}, got {value}")
    return slot, subslot  # type: ignore[return-value]


def data_length(value: object, *, source: str = "") -> int:
    prefix = f"{source}: " if source else ""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_IO_BYTES:
        raise InvalidArgument(f"{prefix}data_length must be an integer between 1 and {MAX_IO_BYTES}, got {value!r}")
    return value
