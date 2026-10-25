# Hilscher cifX/netX controller backend

`PROFINET_BACKEND=cifx` implements raw submodule I/O through the Linux
Hilscher cifX userspace API. The netX PROFINET IO Controller firmware owns
cyclic frames, application relationships and watchdog behavior. The MCP process
is an on-demand process-image client, not a realtime loop or commissioning tool.
The existing `network` backend continues to provide DCP discovery/configuration.
No device, firmware or driver is installed by this package.

## Commissioned prerequisites

Use an exclusively available UIO cifX/netX card, already running PROFINET IO
Controller V3.2 firmware and an established, engineered AR/configuration. This
implementation deliberately excludes VFIO, RAM boot and auto/flash startup
because driver initialization can otherwise reset hardware or download files.
The selected UIO name must report `netx,donttouch,...` (or the corresponding
vendor UIO device prefix). Card selection uses the vendor's enumerated card
index, not a guessed UIO number; its actual descriptor is checked after locked
selection. The opened channel is checked against the map's exact device number,
serial number, firmware name and four-component version before any image access.

An administrator must provision the firmware, module topology, GSDML-derived
addresses, IOCR offsets, AR handles and baseline output status bytes externally.
In particular, output IOCS bytes for input submodules and any unmapped output
provider statuses must already have their intended commissioned values. Reads
never change IOCS or any output bytes. A write changes only the mapped output
payload and its immediately following IOPS byte (GOOD=0x80) in one API call.
All other output bytes are preserved. The firmware's host-watchdog policy must
permit on-demand image access; this server does not keep a realtime watchdog
alive or silently restart it. If the engineering requires a continuous host
loop, that requirement is not fulfilled by an MCP caller.

Use the reviewed nxdrvlinux ABI, pinned for verification at
[`c4a1f67`](https://github.com/HilscherAutomation/nxdrvlinux/tree/c4a1f67acbcc03420edaae30952c5ff5f57e30c5).
Use the reviewed shared-library build with `VIRTETH=OFF`, `TIME=OFF` and `DMA=OFF` (upstream defaults).
VIRTETH initialization cleans up host TAP interfaces globally even before processing
card settings; TIME initialization may change the device clock even in donttouch
mode. DMA builds can disable previously enabled DMA when no device.conf is present.
The binding refuses their exported feature symbols before card access; a channel
already running DMA is also refused.
Custom builds that hide/alter these symbols are outside the supported ABI.
The shared driver/firmware and their licenses are external deployment dependencies.
The package contains independently written bindings and tests; it does not copy
or distribute the vendor driver/toolkit implementation.

## Settings

| Setting | Meaning |
| --- | --- |
| `PROFINET_BACKEND=cifx` | Select this backend; no fallback to simulation |
| `PROFINET_CIFX_LIBRARY` | Absolute path to the supplied `libcifx.so` |
| `PROFINET_CIFX_MAP_FILE` | Absolute path to the reviewed JSON process map |
| `PROFINET_CIFX_BASE_DIR` | Existing, empty directory dedicated to this client; default `/opt/cifx` is refused if nonempty |
| `PROFINET_CIFX_CARD_NUMBER` | Vendor enumeration index, 0..255; default 0 |
| `PROFINET_CIFX_CHANNEL` | Channel index, 0..15; default 0 |
| `PROFINET_CIFX_INIT_ENABLED` | Explicit attach/initialization opt-in, default false |
| `PROFINET_WRITES_ENABLED` | Separate physical-output gate, default false |
| `PROFINET_TIMEOUT` | One call deadline, including initialization and native I/O |

The initialization gate, like every `PROFINET_*_ENABLED` gate, is accepted only
from the real environment or an explicitly selected `--env-file`. An implicit
project `.env` cannot enable it. The empty base directory prevents the SDK from
loading firmware/configuration files or `device.conf` options such as DMA and
host Ethernet interface creation. SDK logs use C `stderr`, preserving MCP stdout.
No automatic scan is requested: one locked native descriptor is passed to
`cifXDriverInit(NOSCAN)` as the single user card. No API call sets bus/host state,
resets a channel, downloads firmware or changes the watchdog.

`ping` only inspects local settings. `get_connection_status` does not initialize
the driver; before the first enabled I/O call, reachability remains unknown.
After initialization it reads identity, state and area information. Failed
initialization closes every acquired handle/descriptor and requires operator
review plus server restart, rather than another automatic initialization attempt.

## Process map

[Example map](../examples/cifx-process-image.json) contains illustrative values,
not defaults suitable for a real card. Replace every identity, size, AR and
offset from the actual commissioned project; do not infer offsets from GSDML
lengths alone. Version 1 maps the controller's single input and output area 0.

Each entry identifies `device_name`, `slot`, `subslot`, `ar` (1..128),
`direction` (`input` or `output`), byte `offset`, exact `length` (1..1439), and
`status_offset` in the **input** image. For input data, that status is its IOPS,
immediately after its payload. For output data, it is the device's returned
IOCS; the transmitted IOPS is automatically appended at output `offset+length`.
Duplicate addresses, inconsistent device/AR identity, overlapping payload/status
ranges, unknown fields, nonintegral values, and out-of-bounds offsets are refused.
Input and output are separate address spaces. A call must match the configured
submodule length exactly; arbitrary process-image offsets are never exposed.

`input_size` and `output_size` must match `xChannelIOInfo`, and each area is bounded
to 64 KiB. Maps are bounded to 64 KiB and 256 entries. `bit_list_offset` names the
start of the controller's 48-byte configured/active/diagnosis AR bit lists:
configured at +0, active at +16, diagnosis at +32. AR N uses byte `(N-1)//8` and
bit `(N-1)%8`. These lists cannot overlap mapped data/status bytes.
`max_age_ms` (1..10000) bounds the elapsed freshness handshake and any subsequent
quality-to-write delay; it is not a device acquisition timestamp.

## Results and failure behavior

Only buffered host-controlled I/O (mode 0/4) is accepted. The first full input
read discards the previous DPM contents and requests an update. The second read
waits for that firmware update; payload, quality and AR bits come from one
snapshot. Identity/state is rechecked around the read. Bus-off, channel errors,
new/restart-required configuration, nonexclusive channel access, mismatched
sizes/modes, expired handshakes, inactive ARs, diagnosis/configuration differences,
and bad/unknown IOPS/IOCS fail closed. This commissioned profile has one status
byte per submodule: bit 0 indicating an extension, or nonzero reserved bits 1..4,
is rejected even when bit 7 says GOOD. Bits 5..6 (instance) are preserved; their
cause interpretation applies to BAD status. For reads, IOPS describes the remote
provider's input data; before writes, received IOCS describes the remote consumer
of the output data. Neither proves physical actuation. No cached payload is
returned on error.

Read metadata reports `quality`, `iops`, `ar`, `age_ms`, `complete`, and
`freshness: "new_firmware_handshake"`. This establishes freshness relative to
firmware exchange, not a synchronized sensor timestamp or functional safety claim.
Write metadata reports `iocs`, `ar`, `confirmation_scope:
"controller_process_image"` and `device_applied: null`. `outcome: "written"`
means the API accepted the image update; it does not prove physical actuation.
A nonzero write return, deadline or cancellation after possible invocation is
uncertain. The write is invoked at most once and never replayed automatically.

A cancelled native call keeps the operation lease until its worker actually
returns. Another call cannot start a second native attempt or close that worker's
session. Shutdown requests cleanup, but native handles are closed only by the
owner once native execution has stopped. Initialization has no vendor timeout;
MCP still answers on its deadline and retains ownership of the blocked worker.
An operator may need to stop a process whose native library never returns.

## Verification and remaining hardware validation

Tests compile a C API fixture and exercise actual ctypes calls, ABI layouts,
map rejection, gates, state/quality failures, snapshot freshness, single writes,
uncertain outcomes, cancellation, cleanup and real MCP envelopes. A C compiler
(`cc`) is required for the Python test suite; no SDK or hardware is required for
the default fixture. To independently compile the same tests against the actual
reviewed vendor headers:

```sh
CIFX_SDK_HEADERS=/path/to/nxdrvlinux uv run --directory profinet-python pytest tests/test_cifx.py
```

CI fetches the exact pinned commit and runs this check as a separate required
verification job. Nothing from that SDK is built as a driver or installed.
Physical-card attachment, firmware-specific cycle/watchdog behavior, real device
quality changes and physical outputs still require an authorized isolated
hardware acceptance test. Formal PI conformance is not claimed. Record services,
alarm consumption, live commissioning, additional firmware generations, VFIO and
multi-area/multi-card process images remain unsupported.
