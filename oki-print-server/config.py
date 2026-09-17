"""Configuration loading and validation for the OKI print server."""

from __future__ import annotations

import ast
import codecs
import dataclasses
import logging
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and older
    tomllib = None


DEFAULT_CONFIG_PATH = Path("/etc/oki-print-server/config.toml")


class ConfigError(ValueError):
    """Raised when the configuration file is invalid."""


@dataclasses.dataclass(frozen=True)
class NetworkConfig:
    host: str = "0.0.0.0"
    port: int = 5005
    max_packet_bytes: int = 8192


@dataclasses.dataclass(frozen=True)
class PrinterConfig:
    device: str = "/dev/usb/lp0"
    encoding: str = "cp437"
    trailing_lines: int = 2
    form_feed: bool = False
    retry_seconds: float = 3.0


@dataclasses.dataclass(frozen=True)
class QueueConfig:
    max_jobs: int = 100


@dataclasses.dataclass(frozen=True)
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    max_text_bytes: int = 8192


@dataclasses.dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"


@dataclasses.dataclass(frozen=True)
class Config:
    network: NetworkConfig = dataclasses.field(default_factory=NetworkConfig)
    printer: PrinterConfig = dataclasses.field(default_factory=PrinterConfig)
    queue: QueueConfig = dataclasses.field(default_factory=QueueConfig)
    web: WebConfig = dataclasses.field(default_factory=WebConfig)
    logging: LoggingConfig = dataclasses.field(default_factory=LoggingConfig)


def _parse_scalar(value: str, line_number: int) -> Any:
    """Parse the small TOML scalar subset used by this application."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            parsed = ast.literal_eval(value)
            if not isinstance(parsed, str):
                raise ValueError("not a string")
            return parsed
        except (SyntaxError, ValueError) as exc:
            raise ConfigError(f"invalid string on line {line_number}: {exc}") from exc
    if value in ("true", "false"):
        return value == "true"
    try:
        return float(value) if "." in value else int(value)
    except ValueError as exc:
        raise ConfigError(f"invalid value on line {line_number}: {value!r}") from exc


def _minimal_toml_loads(text: str) -> Dict[str, Dict[str, Any]]:
    """Fallback parser for old Pi OS Python versions without tomllib.

    It intentionally accepts only tables and the string, boolean, integer, and
    decimal values used by config.example.toml.
    """
    result: Dict[str, Dict[str, Any]] = {}
    current: Optional[Dict[str, Any]] = None
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if not section or "." in section:
                raise ConfigError(f"invalid section on line {line_number}")
            current = result.setdefault(section, {})
            continue
        if current is None or "=" not in line:
            raise ConfigError(f"invalid configuration on line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError(f"missing key on line {line_number}")
        current[key] = _parse_scalar(value, line_number)
    return result


def _read_toml(path: Path) -> Dict[str, Any]:
    data = path.read_bytes()
    if tomllib is not None:
        try:
            return tomllib.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot parse {path}: {exc}") from exc
    try:
        return _minimal_toml_loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} is not UTF-8: {exc}") from exc


def _section(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    section = data.get(name, {})
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}] must be a table")
    return section


def _reject_unknown(data: Dict[str, Any]) -> None:
    allowed = {
        "network": {"host", "port", "max_packet_bytes"},
        "printer": {
            "device",
            "encoding",
            "trailing_lines",
            "form_feed",
            "retry_seconds",
        },
        "queue": {"max_jobs"},
        "web": {"enabled", "host", "port", "max_text_bytes"},
        "logging": {"level"},
    }
    unknown_sections = set(data) - set(allowed)
    if unknown_sections:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown_sections))}")
    for section_name, valid_keys in allowed.items():
        unknown_keys = set(_section(data, section_name)) - valid_keys
        if unknown_keys:
            names = ", ".join(sorted(unknown_keys))
            raise ConfigError(f"unknown key(s) in [{section_name}]: {names}")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load a configuration file, or return safe defaults if it is absent."""
    data = _read_toml(path) if path.exists() else {}
    _reject_unknown(data)
    try:
        config = Config(
            network=NetworkConfig(**_section(data, "network")),
            printer=PrinterConfig(**_section(data, "printer")),
            queue=QueueConfig(**_section(data, "queue")),
            web=WebConfig(**_section(data, "web")),
            logging=LoggingConfig(**_section(data, "logging")),
        )
    except TypeError as exc:
        raise ConfigError(f"invalid configuration type: {exc}") from exc

    if not isinstance(config.network.host, str) or not config.network.host:
        raise ConfigError("network.host must be a non-empty string")
    if type(config.network.port) is not int or not 1 <= config.network.port <= 65535:
        raise ConfigError("network.port must be an integer from 1 to 65535")
    if type(config.network.max_packet_bytes) is not int or not 1 <= config.network.max_packet_bytes <= 65507:
        raise ConfigError("network.max_packet_bytes must be an integer from 1 to 65507")
    if not isinstance(config.printer.device, str) or not config.printer.device:
        raise ConfigError("printer.device must be a non-empty string")
    if not isinstance(config.printer.encoding, str):
        raise ConfigError("printer.encoding must be a string")
    try:
        codecs.lookup(config.printer.encoding)
    except LookupError as exc:
        raise ConfigError(f"unknown printer encoding: {config.printer.encoding}") from exc
    if type(config.printer.trailing_lines) is not int or config.printer.trailing_lines < 0:
        raise ConfigError("printer.trailing_lines must be a non-negative integer")
    if type(config.printer.form_feed) is not bool:
        raise ConfigError("printer.form_feed must be true or false")
    if not isinstance(config.printer.retry_seconds, (int, float)) or isinstance(config.printer.retry_seconds, bool) or config.printer.retry_seconds <= 0:
        raise ConfigError("printer.retry_seconds must be a positive number")
    if type(config.queue.max_jobs) is not int or config.queue.max_jobs < 1:
        raise ConfigError("queue.max_jobs must be a positive integer")
    if type(config.web.enabled) is not bool:
        raise ConfigError("web.enabled must be true or false")
    if not isinstance(config.web.host, str) or not config.web.host:
        raise ConfigError("web.host must be a non-empty string")
    if type(config.web.port) is not int or not 1 <= config.web.port <= 65535:
        raise ConfigError("web.port must be an integer from 1 to 65535")
    if type(config.web.max_text_bytes) is not int or not 1 <= config.web.max_text_bytes <= 65507:
        raise ConfigError("web.max_text_bytes must be an integer from 1 to 65507")
    if not isinstance(config.logging.level, str) or config.logging.level.upper() not in logging._nameToLevel:
        raise ConfigError("logging.level must be a valid level such as INFO or DEBUG")
    return config
