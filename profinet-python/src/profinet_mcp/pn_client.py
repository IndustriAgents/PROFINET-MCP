"""Compatibility import; implementation lives in adapters.pn_client."""

import sys

from .adapters import pn_client as _implementation

sys.modules[__name__] = _implementation
