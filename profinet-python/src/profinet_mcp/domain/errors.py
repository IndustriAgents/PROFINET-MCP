from __future__ import annotations

from ..config import ProfinetConfig


class ProfinetClientError(RuntimeError):
    """A PROFINET operation failed; the message is shown to the model."""


class OperationRefused(ProfinetClientError):
    """Refused by a safety switch (writes or configuration commands disabled), or rate-limited."""


WRITES_DISABLED = (
    "Writes are disabled, so nothing was sent. The "
    "server is read-only by default; set "
    "PROFINET_WRITES_ENABLED=true in the server's "
    "environment to allow write_io_data and "
    "write_device_by_alias."
)
CONFIG_CMD_TOOLS = "set_device_name, set_device_ip and identify_device"


def config_cmds_refusal(config: ProfinetConfig) -> str | None:
    """Why a DCP configuration command (DCP Set) is refused, or None if both switches are on.

    These commands change a device, so they need the general write switch *and* their own switch.
    """
    missing = [
        name
        for name, enabled in (
            ("PROFINET_WRITES_ENABLED", config.writes_enabled),
            ("PROFINET_CONFIG_CMDS_ENABLED", config.config_cmds_enabled),
        )
        if not enabled
    ]
    if not missing:
        return None
    return (
        "DCP configuration commands are disabled, so "
        "nothing was sent. "
        f"{CONFIG_CMD_TOOLS}"
        " need both PROFINET_WRITES_ENABLED=true and "
        "PROFINET_CONFIG_CMDS_ENABLED=true in the "
        "server's environment; not set: "
        f"{', '.join(missing)}"
        "."
    )
