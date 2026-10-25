"""ctypes declarations for nxdrvlinux c4a1f67 (Linux C ABI, not a driver implementation)."""

from __future__ import annotations

import ctypes as C

U32 = C.c_uint32
HANDLE = C.c_void_p


class UnsupportedLibraryError(RuntimeError):
    """A known optional driver build changes state during attachment."""


class DmaBuffer(C.Structure):
    _fields_ = [("size", U32), ("physical_address", U32), ("buffer", HANDLE)]


class Device(C.Structure):
    _fields_ = [
        ("dpm", HANDLE),
        ("dpmaddr", C.c_ulong),
        ("dpmlen", C.c_ulong),
        ("uio_num", C.c_int),
        ("uio_fd", C.c_int),
        ("pci_card", C.c_int),
        ("force_ram", C.c_int),
        ("notify", HANDLE),
        ("userparam", HANDLE),
        ("extmem", HANDLE),
        ("extmemaddr", C.c_ulong),
        ("extmemlen", C.c_ulong),
        ("dma_buffer_cnt", U32),
        ("dma_buffer", DmaBuffer * 8),
        ("hwif_init", HANDLE),
        ("hwif_deinit", HANDLE),
        ("hwif_read", HANDLE),
        ("hwif_write", HANDLE),
    ]


class LinuxInit(C.Structure):
    _fields_ = [
        ("init_options", C.c_int),
        ("base_dir", C.c_char_p),
        ("poll_interval", C.c_ulong),
        ("poll_priority", C.c_int),
        ("trace_level", C.c_ulong),
        ("user_card_cnt", C.c_int),
        ("user_cards", C.POINTER(Device)),
        ("iCardNumber", C.c_int),
        ("fEnableCardLocking", C.c_int),
        ("poll_StackSize", C.c_int),
        ("poll_schedpolicy", C.c_int),
        ("logfd", HANDLE),
    ]


class ChannelInfo(C.Structure):
    _pack_ = 1
    _fields_ = [
        ("board_name", C.c_char * 16),
        ("board_alias", C.c_char * 16),
        ("device_number", U32),
        ("serial_number", U32),
        ("fw_major", C.c_uint16),
        ("fw_minor", C.c_uint16),
        ("fw_build", C.c_uint16),
        ("fw_revision", C.c_uint16),
        ("fw_name_length", C.c_uint8),
        ("fw_name", C.c_uint8 * 63),
        ("fw_year", C.c_uint16),
        ("fw_month", C.c_uint8),
        ("fw_day", C.c_uint8),
        ("channel_error", U32),
        ("open_count", U32),
        ("put_count", U32),
        ("get_count", U32),
        ("mailbox_size", U32),
        ("input_areas", U32),
        ("output_areas", U32),
        ("handshake_size", U32),
        ("netx_flags", U32),
        ("host_flags", U32),
        ("host_cos_flags", U32),
        ("device_cos_flags", U32),
    ]


class IOInfo(C.Structure):
    _pack_ = 1
    _fields_ = [("total_size", U32), ("reserved", U32), ("mode", U32)]


def load_library(path: str):
    library = C.CDLL(path)
    # These optional builds perform side effects during initialization even in
    # donttouch mode: global TAP cleanup (VIRTETH), device clock changes (TIME).
    # The reviewed upstream shared library exports these feature symbols.
    for name in ("cifx_cifxeth_scan", "cifxeth_sys_cleanup", "cifXInitTime", "USER_GetDMAMode"):
        if hasattr(library, name):
            raise UnsupportedLibraryError("cifX requires the reviewed VIRTETH=OFF, TIME=OFF, DMA=OFF build")
    signatures = {
        "cifXFindDevice": ([C.c_int, C.c_int], C.POINTER(Device)),
        "cifXDeleteDevice": ([C.POINTER(Device)], None),
        "cifXDriverInit": ([C.POINTER(LinuxInit)], C.c_int32),
        "cifXDriverDeinit": ([], None),
        "xDriverOpen": ([C.POINTER(HANDLE)], C.c_int32),
        "xDriverClose": ([HANDLE], C.c_int32),
        "xChannelOpen": ([HANDLE, C.c_char_p, U32, C.POINTER(HANDLE)], C.c_int32),
        "xChannelClose": ([HANDLE], C.c_int32),
        "xChannelInfo": ([HANDLE, U32, HANDLE], C.c_int32),
        "xChannelIOInfo": ([HANDLE, U32, U32, U32, HANDLE], C.c_int32),
        "xChannelBusState": ([HANDLE, U32, C.POINTER(U32), U32], C.c_int32),
        "xChannelIORead": ([HANDLE, U32, U32, U32, HANDLE, U32], C.c_int32),
        "xChannelIOWrite": ([HANDLE, U32, U32, U32, HANDLE, U32], C.c_int32),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(library, name)
        fn.argtypes = args
        fn.restype = result
    return library


def stderr_file():
    """C FILE* for SDK logging; keep the MCP stdout stream free of native logs."""
    runtime = C.CDLL(None)
    for symbol in ("stderr", "__stderrp"):
        try:
            return HANDLE.in_dll(runtime, symbol).value
        except ValueError:
            continue
    raise RuntimeError("The C runtime stderr stream is unavailable")
