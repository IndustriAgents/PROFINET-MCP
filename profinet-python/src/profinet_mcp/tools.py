"""Compatibility imports; implementation lives in domain, application and protocol modules."""

import functools as functools
import json as json
import logging as logging
from collections.abc import Awaitable as Awaitable
from collections.abc import Callable as Callable
from dataclasses import dataclass as dataclass
from pathlib import Path as Path
from typing import Annotated as Annotated
from typing import Any as Any

from pydantic import Field as Field
from pydantic import ValidationError as ValidationError

from profinet_mcp import __version__ as __version__
from profinet_mcp import validation as validation
from profinet_mcp.application.resources import ToolResources as ToolResources
from profinet_mcp.backends import BackendNotConfigured as BackendNotConfigured
from profinet_mcp.backends import ClientCancelled as ClientCancelled
from profinet_mcp.backends import DeviceRejected as DeviceRejected
from profinet_mcp.backends import OperationNotImplemented as OperationNotImplemented
from profinet_mcp.backends import OperationRefused as OperationRefused
from profinet_mcp.backends import OutcomeUnknown as OutcomeUnknown
from profinet_mcp.backends import ProfinetClientError as ProfinetClientError
from profinet_mcp.backends import RateLimited as RateLimited
from profinet_mcp.backends import RequestNotSent as RequestNotSent
from profinet_mcp.backends import tool_call as tool_call
from profinet_mcp.config import ProfinetConfig as ProfinetConfig
from profinet_mcp.domain.errors import WRITES_DISABLED as WRITES_DISABLED
from profinet_mcp.domain.errors import config_cmds_refusal as config_cmds_refusal
from profinet_mcp.domain.model import CHANGE_TOOLS as CHANGE_TOOLS
from profinet_mcp.domain.model import Alias as Alias
from profinet_mcp.domain.model import ByteValues as ByteValues
from profinet_mcp.domain.model import DataLength as DataLength
from profinet_mcp.domain.model import DeviceMac as DeviceMac
from profinet_mcp.domain.model import DeviceName as DeviceName
from profinet_mcp.domain.model import DeviceRef as DeviceRef
from profinet_mcp.domain.model import DiscoveryTimeout as DiscoveryTimeout
from profinet_mcp.domain.model import Envelope as Envelope
from profinet_mcp.domain.model import FilePath as FilePath
from profinet_mcp.domain.model import IdentifySeconds as IdentifySeconds
from profinet_mcp.domain.model import IPv4 as IPv4
from profinet_mcp.domain.model import Slot as Slot
from profinet_mcp.domain.model import StationName as StationName
from profinet_mcp.domain.model import Subslot as Subslot
from profinet_mcp.domain.model import envelope as envelope
from profinet_mcp.domain.model import invalid_arguments as invalid_arguments
from profinet_mcp.domain.model import log as log
from profinet_mcp.domain.model import outcome as outcome
from profinet_mcp.gsd_parser import GSDMLError as GSDMLError
from profinet_mcp.gsd_parser import GSDParser as GSDParser
from profinet_mcp.infrastructure.maps import DeviceMap as DeviceMap
from profinet_mcp.pn_client import ProfinetClient as ProfinetClient

from .protocol.support import LOCAL as LOCAL
from .protocol.support import READ as READ
from .protocol.support import WRITE as WRITE
from .protocol.support import FastMCP as FastMCP
from .protocol.support import ToolAnnotations as ToolAnnotations
from .protocol.support import request_cancel_scope as request_cancel_scope
from .protocol.tools import register_tools as register_tools
