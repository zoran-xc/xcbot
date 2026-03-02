"""Channel registry for pluggable channel initialization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from xcbot.bus.queue import MessageBus
from xcbot.channels.base import BaseChannel
from xcbot.config.schema import Config


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    is_enabled: Callable[[Config], bool]
    build: Callable[[Config, MessageBus, Path], BaseChannel]


def _build_telegram(config: Config, bus: MessageBus, workspace: Path) -> BaseChannel:
    from xcbot.channels.telegram import TelegramChannel

    return TelegramChannel(
        config.channels.telegram,
        bus,
        workspace,
        groq_api_key=config.providers.groq.api_key,
    )


def _build_whatsapp(config: Config, bus: MessageBus, workspace: Path) -> BaseChannel:
    from xcbot.channels.whatsapp import WhatsAppChannel

    return WhatsAppChannel(config.channels.whatsapp, bus, workspace)


def _build_discord(config: Config, bus: MessageBus, workspace: Path) -> BaseChannel:
    from xcbot.channels.discord import DiscordChannel

    return DiscordChannel(config.channels.discord, bus, workspace)


def _build_feishu(config: Config, bus: MessageBus, workspace: Path) -> BaseChannel:
    from xcbot.channels.feishu import FeishuChannel

    return FeishuChannel(config.channels.feishu, bus, workspace)


def _build_mochat(config: Config, bus: MessageBus, workspace: Path) -> BaseChannel:
    from xcbot.channels.mochat import MochatChannel

    return MochatChannel(config.channels.mochat, bus, workspace)


def _build_dingtalk(config: Config, bus: MessageBus, workspace: Path) -> BaseChannel:
    from xcbot.channels.dingtalk import DingTalkChannel

    return DingTalkChannel(config.channels.dingtalk, bus, workspace)


def _build_email(config: Config, bus: MessageBus, workspace: Path) -> BaseChannel:
    from xcbot.channels.email import EmailChannel

    return EmailChannel(config.channels.email, bus, workspace)


def _build_slack(config: Config, bus: MessageBus, workspace: Path) -> BaseChannel:
    from xcbot.channels.slack import SlackChannel

    return SlackChannel(config.channels.slack, bus, workspace)


def _build_qq(config: Config, bus: MessageBus, workspace: Path) -> BaseChannel:
    from xcbot.channels.qq import QQChannel

    return QQChannel(config.channels.qq, bus, workspace)


def _build_matrix(config: Config, bus: MessageBus, workspace: Path) -> BaseChannel:
    from xcbot.channels.matrix import MatrixChannel

    return MatrixChannel(config.channels.matrix, bus, workspace)


CHANNEL_SPECS: tuple[ChannelSpec, ...] = (
    ChannelSpec(
        name="telegram",
        is_enabled=lambda c: c.channels.telegram.enabled,
        build=_build_telegram,
    ),
    ChannelSpec(
        name="whatsapp",
        is_enabled=lambda c: c.channels.whatsapp.enabled,
        build=_build_whatsapp,
    ),
    ChannelSpec(
        name="discord",
        is_enabled=lambda c: c.channels.discord.enabled,
        build=_build_discord,
    ),
    ChannelSpec(
        name="feishu",
        is_enabled=lambda c: c.channels.feishu.enabled,
        build=_build_feishu,
    ),
    ChannelSpec(
        name="mochat",
        is_enabled=lambda c: c.channels.mochat.enabled,
        build=_build_mochat,
    ),
    ChannelSpec(
        name="dingtalk",
        is_enabled=lambda c: c.channels.dingtalk.enabled,
        build=_build_dingtalk,
    ),
    ChannelSpec(
        name="email",
        is_enabled=lambda c: c.channels.email.enabled,
        build=_build_email,
    ),
    ChannelSpec(
        name="slack",
        is_enabled=lambda c: c.channels.slack.enabled,
        build=_build_slack,
    ),
    ChannelSpec(
        name="qq",
        is_enabled=lambda c: c.channels.qq.enabled,
        build=_build_qq,
    ),
    ChannelSpec(
        name="matrix",
        is_enabled=lambda c: c.channels.matrix.enabled,
        build=_build_matrix,
    ),
)


def iter_enabled(config: Config) -> tuple[ChannelSpec, ...]:
    return tuple(spec for spec in CHANNEL_SPECS if spec.is_enabled(config))
