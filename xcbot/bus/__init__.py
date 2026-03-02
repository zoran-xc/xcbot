"""Message bus module for decoupled channel-agent communication."""

from xcbot.bus.events import InboundMessage, OutboundMessage
from xcbot.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
