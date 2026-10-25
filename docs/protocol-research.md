# PROFINET protocol assessment and implementation evidence

Research date: 2026-10-25. Baseline: `7ea97fa`. Supported native operations are
DCP discovery and gated unicast DCP Set, plus an optional Hilscher cifX/netX
process-image adapter for a precommissioned PROFINET IO Controller V3.2.

## Primary sources

| Source | Version / sections inspected | Consequence |
| --- | --- | --- |
| [PI GSD/GSDML documentation](https://www.profibus.com/products/gsd-files) | Public online description, accessed 2026-10-25 | GSDML supplies engineering metadata, not a reachable process image. |
| [Scapy DCP API](https://scapy.readthedocs.io/en/latest/api/scapy.contrib.pnio_dcp.html) and [source](https://github.com/secdev/scapy/blob/master/scapy/contrib/pnio_dcp.py) | Online 2.8.0 docs and locally locked Scapy code; Set header, qualifier, Control/Response and block-error definitions | Independent encoder/decoder for packet fixture cross-checks; source does not itself establish device compatibility. |
| [Wireshark DCP dissector](https://github.com/wireshark/wireshark/blob/master/plugins/epan/profinet/packet-pn-dcp.c) | Public upstream source; Control/Signal and Control/Response | Signal carries a fixed flash request, not an arbitrary duration field. |
| [Hilscher IO-Link Configurator manual](https://www.hilscher.com/fileadmin/cms_upload/en-US/Resources/pdf/netFIELD_App_IO-Link_Configurator_OI_1_EN.pdf) | DOC230601OI01EN revision 1, 2024-08, section 8.1.3 | Documents DCP commissioning and a three-second signal sequence; actual hardware timing is not measured here. |
| [Siemens communication manual](https://support.industry.siemens.com/cs/attachments/99007729/Communication_en-US.pdf) | 04/2014, section 5.3, IP configuration table | Permanent and temporary settings differ across restart; this API explicitly requests permanent name/IP settings. |
| [Hilscher IO-controller API](https://www.hilscher.com/fileadmin/cms_upload/de/Resources/pdf/PROFINET_IO_Controller_V3_Protocol_API_06_EN.pdf) | Revision 6, 2017-09; DCP and controller API | A controller integration requires a concrete driver/firmware API, not a DCP socket. |
| [p-net upstream](https://github.com/rtlabs-com/p-net) | README and evaluation scope, accessed 2026-10-25 | It is a device stack, not the IO-controller role required here; the public evaluation tree lacks ports. |

The full licensed normative specification was not available in this workspace.
Sources above support implementation choices; no conformance certification is
claimed. Public documentation was paraphrased; no upstream implementation was
copied into this MIT source.

## Capability and gap matrix

| Canonical tool | Native / mock capability | Outcome and constraints | Evidence |
| --- | --- | --- | --- |
| `ping` | Local backend capability inventory added | No probe; support/gates differ from reachability | MCP and installed Python/npm smoke tests |
| `discover_devices` | DCP Identify / mock list | Rate limited, serialized native lease, bounded capture and explicit incompleteness | Packet fixtures, overflow and cancellation tests |
| `get_device_info` | Discovery lookup / mock lookup | Cached answers report age; configuration invalidates cache | Existing cache tests plus configuration invalidation regression |
| `set_device_name` | New native single unicast Set / existing mock | Both config gates; permanent value; correlated protocol acknowledgement only | Hand-written request assertions and Scapy response fixtures |
| `set_device_ip` | New native single unicast Set / existing mock | IP/mask/gateway validation; permanent value; no resend | Independent Scapy decode and acknowledgement fixtures |
| `identify_device` | New native Signal / existing mock | One sequence; native duration_s=3 only; device controls timing | Payload fixture and unsupported-duration regression |
| `read_io_data`, `write_io_data` | Mock; cifX commissioned input/output image | Exact mapped submodule, fresh handshake, AR and IOPS/IOCS validation; one write | Compiled native ABI fixture and real MCP tests |
| `list_devices`, `load_gsd_file` | Local alias/GSDML data | Metadata is not proof of hardware availability | Existing parser/map tests |
| `get_connection_status` | Mock TCP probe; native reachability unknown | Never fabricate a live device from interface settings | Existing backend/status tests |
| Cyclic frames / AR / watchdog | Hilscher controller firmware owns these | MCP is an on-demand process-image client | Reviewed public controller API and vendor driver source |
| Record data, alarms, LLDP topology, security classes, live commissioning | Not implemented | Outside the supported mapped-I/O and DCP scope | Explicit unsupported scope |

## Native execution design

`dcp_set.py` implements only one-block unicast Set operations, with fresh
transaction identifiers and strict source MAC, destination MAC, FrameID,
service, response type, option/suboption, length and block-error checks.
Broadcast, multicast and all-zero mutation targets fail before dispatch.
Reserved/malformed response codes, missing replies and conflicting acknowledgements
are uncertain outcomes. Only recognized explicit rejections establish rejection.
No failed control path resends the command.

`adapters/dcp_runtime.py` owns a native interface lease independently of the MCP
coroutine. Cancellation marks an attempt stopped but retains its lease until the
worker actually exits. A queued cancelled worker cannot dispatch. The real Scapy
transport checks the attempt immediately before send, after potentially blocking
capture startup. Shutdown and absolute deadlines use the same fence.

Discovery and configuration share the interface lease. A scan cannot overlap a
cancelled native exchange. Cache generations prevent an older lookup from
replacing state after configuration starts. Configuration invalidates discovery
metadata even when its acknowledgement is lost.

Scapy stores no unbounded packet list. At most 1,024 matching frames and 1 MiB
are retained per capture. Discovery reports `complete=false` and
`capture_truncated=true` on overflow; Set refuses to infer success from a
truncated capture. Nonmatching traffic is not accumulated.

## Agent contract and migration

Call `ping` before selecting a device workflow. Its `capabilities` reports the
canonical operation map, `scope: "backend_support"`, actual backend,
`simulated` and `device_verified: false`. `enabled` considers native configuration
gates only. MCP policy and device restrictions still apply.

Name and IP tools request permanent storage. `meta.confirmation` is
`protocol_acknowledgement`; `physical_completion_verified` is false. Existing
`meta.outcome: "written"` means the single service was acknowledged, not that a
plant process completed. Query the device deliberately to verify the intended
configuration after an uncertain result; never automatically resend.

`identify_device` now defaults to three seconds instead of five. Network calls
with other explicit durations fail before sending, because the wire service
cannot represent the mock's arbitrary duration. The mock continues to accept
explicit durations. Native results also report `duration_verified: false`.

Examples (only on an explicitly authorized interface with both native gates and
MCP policy allowing configuration): `set_device_name` with an explicit unicast
MAC and lowercase station name; `set_device_ip` with that MAC and validated IPv4
suite; `identify_device` with the MAC and `duration_s: 3`.

## Verification and remaining work

The baseline passed before edits. Verification includes Python/unit/native packet
and MCP tests, loopback mock integration, cancellation/failure cases, lint and
contract checks, real wheel/sdist/npm builds, installed artifact MCP sessions and
package metadata checks. Smoke tests remove inherited protocol endpoints,
policies and map paths and invoke only local status with the native backend off.

No live PROFINET packets were transmitted and no physical device was configured.
DCP interoperability and LED timing remain unverified on hardware.

The selected controller integration is Hilscher Linux cifX/netX with precommissioned
PROFINET IO Controller V3.2 firmware. Its software adapter is implemented and tested;
actual card/driver/firmware provisioning and isolated hardware acceptance remain
external requirements, not an unresolved SDK selection. See the
[cifX deployment and verification guide](cifx-controller.md).

## cifX primary-source decisions

- [Official nxdrvlinux source](https://github.com/HilscherAutomation/nxdrvlinux/tree/c4a1f67acbcc03420edaae30952c5ff5f57e30c5), inspected at that exact commit: `libcifx/cifxlinux.h`,
  `Toolkit/Common/cifXAPI/cifXUser.h` / `cifXErrors.h`, `cifxlinux.c`,
  `user_linux.c`, `cifXFunctions.c` and `Hil_DualPortMemory.h` define the ABI,
  user-card ownership, donttouch startup, I/O modes and state flags. A cancelled
  Python coroutine cannot cancel a C call. Native ownership therefore survives it.
- [PROFINET IO Controller V3.2 API revision 6 (2017-09)](https://www.hilscher.com/fileadmin/cms_upload/en-US/Resources/pdf/PROFINET_IO_Controller_V3_Protocol_API_06_EN.pdf),
  sections 2.2.1/2.2.2, 3.1.1/3.1.7 and 3.6.1: I/O data, provider/consumer statuses
  and AR state lists are distinct. An input API call requests the next image only
  after copying its current contents, motivating the discard-first read. The
  output API acknowledges an image update rather than physical execution.

The native C fixture is independently written. CI also compiles it against the
pinned upstream headers to detect ABI discrepancies. No upstream driver
implementation or toolkit headers are included in the distribution.
