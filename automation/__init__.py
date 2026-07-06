"""Autonomous fleet bots: mining, trading, contracts, market probing.

All bots run in plain threads, are cancellable via .stop(), and report through
on_log / on_status callbacks so the CLI and TUI can share them.
"""

from .base import BaseBot, BotCancelled
from .contractor import ContractBot
from .fleet import FleetCommander, default_bot_for
from .miner import MinerBot
from .probe import ProbeBot
from .trader import TraderBot

BOT_TYPES = {
    "mine": MinerBot,
    "trade": TraderBot,
    "contract": ContractBot,
    "probe": ProbeBot,
}

__all__ = [
    "BaseBot",
    "BotCancelled",
    "MinerBot",
    "TraderBot",
    "ContractBot",
    "ProbeBot",
    "FleetCommander",
    "default_bot_for",
    "BOT_TYPES",
]
