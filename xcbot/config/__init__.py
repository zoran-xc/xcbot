"""Configuration module for xcbot."""

from xcbot.config.loader import load_config, get_config_path
from xcbot.config.schema import Config

__all__ = ["Config", "load_config", "get_config_path"]
