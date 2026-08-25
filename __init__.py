"""Tenki cloud sandbox terminal backend for hermes-agent.

Installed as a hermes-agent plugin, this package registers ``tenki`` as a
``terminal.backend`` value via the terminal-environment plugin registry.
See README.md for installation and configuration.
"""

from __future__ import annotations


def register(ctx) -> None:
    from .provider import TenkiProvider

    ctx.register_terminal_environment_provider(TenkiProvider())
