"""Messenger abstraction layer — transport-agnostic protocols and registry."""

from ductor_bot.messenger.capabilities import MessengerCapabilities
from ductor_bot.messenger.multi import MultiBotAdapter
from ductor_bot.messenger.notifications import CompositeNotificationService, NotificationService
from ductor_bot.messenger.protocol import BotProtocol
from ductor_bot.messenger.registry import create_bot

__all__ = [
    "BotProtocol",
    "CompositeNotificationService",
    "MessengerCapabilities",
    "MultiBotAdapter",
    "NotificationService",
    "create_bot",
]
