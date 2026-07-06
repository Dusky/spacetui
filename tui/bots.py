"""Compatibility shim: bots now live in the top-level `automation` package."""

from automation import (  # noqa: F401
    BOT_TYPES,
    BaseBot,
    BotCancelled,
    ContractBot,
    MinerBot,
    ProbeBot,
    TraderBot,
    default_bot_for,
)
