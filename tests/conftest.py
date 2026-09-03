"""Pytest bootstrap: stub turbojpeg (camera dep) when the native lib is absent."""

from __future__ import annotations

import sys
import types


def _ensure_turbojpeg_stub() -> None:
    try:
        import turbojpeg  # noqa: F401

        return
    except ImportError:
        pass
    stub = types.ModuleType("turbojpeg")

    class TurboJPEG:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def decode(self, *args, **kwargs):
            raise NotImplementedError

        def encode(self, *args, **kwargs):
            raise NotImplementedError

    stub.TurboJPEG = TurboJPEG
    sys.modules["turbojpeg"] = stub


_ensure_turbojpeg_stub()
