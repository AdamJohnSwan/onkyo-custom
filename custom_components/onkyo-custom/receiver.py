"""Onkyo receiver."""

from __future__ import annotations

from dataclasses import dataclass

import pyeiscpcustom


@dataclass
class Receiver:
    """Onkyo receiver."""

    conn: pyeiscpcustom.Connection
    model_name: str
    identifier: str
    name: str
    discovered: bool


@dataclass
class ReceiverInfo:
    """Onkyo receiver information."""

    host: str
    port: int
    model_name: str
    identifier: str
