"""The interface every chat-app connector implements, and how one is chosen.

A connector knows one chat app's API (``slack.py``, ``discord.py``) and
nothing about the autopilot. The bridge talks to exactly one of them. Each is
standard library only, with HTTP injected so tests never touch the network.

Secrets: a connector gets its bot token from the bridge, which reads it from
the environment variable named by ``chat.<provider>.token_env`` or from
``~/.config/specify/autopilot/<provider>-token``. Tokens never appear in the
config files, the journal or any output.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

PROVIDERS = ("slack", "discord")
# Status tags the bridge applies (Discord forum tags; Slack shows them in the header).
TAG_NAMES = ("run", "epic", "pending", "running", "needs-owner", "merged", "failed", "blocked", "landed")
SECRETS_DIR = Path("~/.config/specify/autopilot")
DISPLAY = {"lead": "lead", "reviewer": "reviewer", "autopilot": "autopilot", "owner": "owner (via agent chat)"}

# (method, url, headers, body) -> (status, headers, body)
Http = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


def display_name(author: str) -> str:
    return DISPLAY.get(author, author)


@dataclass(frozen=True)
class Caps:
    hidden_metadata: bool = False
    tags: bool = False
    edit_status: bool = False
    custom_names: bool = True
    top_level: bool = False
    max_text: int = 2000


@dataclass
class Out:
    author: str
    text: str
    mention_owner: bool = False
    meta: dict[str, str] = field(default_factory=dict)


@dataclass
class Inbound:
    ref: str
    author_id: str
    is_bot: bool
    text: str


class ConnectorError(RuntimeError):
    """A failed chat API call.

    ``retryable``: try again later (rate limit, outage), after ``retry_after``
    seconds when known. ``fatal``: credentials or permissions are wrong; stop
    delivering and report it instead of retrying in a loop.
    """

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None,
                 fatal: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.fatal = fatal


class Connector(Protocol):
    name: str
    caps: Caps

    def check(self) -> dict[str, Any]: ...
    def open_thread(self, title: str, status: str, tags: list[str], first: Out) -> str: ...
    def post(self, thread: str, message: Out) -> list[str]: ...
    def set_status(self, thread: str, title: str, status: str, tags: list[str]) -> None: ...
    def fetch(self, thread: str, cursor: str | None) -> tuple[list[Inbound], str]: ...
    def fetch_top_level(self, cursor: str | None) -> tuple[list[Inbound], str]: ...
    def acknowledge(self, thread: str, ref: str, ok: bool) -> None: ...


def split_text(text: str, limit: int) -> list[str]:
    """Split on line boundaries into chunks no longer than ``limit``."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return [c.rstrip("\n") for c in chunks if c.strip()]


def urllib_http(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https API hosts
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ConnectorError(f"network error: {exc}", retryable=True) from exc


def read_token(provider: str, chat_config: dict[str, Any]) -> str:
    """The bot token: the configured environment variable, else the private token file."""
    env_name = str(chat_config.get(provider, {}).get("token_env") or "").strip()
    if env_name and os.environ.get(env_name, "").strip():
        return os.environ[env_name].strip()
    path = (SECRETS_DIR / f"{provider}-token").expanduser()
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    raise ConnectorError(
        f"no {provider} bot token: set ${env_name or 'the token_env variable'} or write it to {path} (mode 600)",
        fatal=True,
    )


def secret_file(name: str) -> Path:
    path = (SECRETS_DIR / name).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_secret(name: str, data: dict[str, Any]) -> None:
    path = secret_file(name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def read_secret(name: str) -> dict[str, Any] | None:
    path = secret_file(name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def make_connector(config: dict[str, Any], http: Http | None = None, token: str | None = None) -> Connector:
    chat = config["chat"]
    provider = chat["provider"]
    if provider not in PROVIDERS:
        raise ConnectorError(f"chat.provider is {provider!r}; set slack or discord", fatal=True)
    token = token or read_token(provider, chat)
    if provider == "slack":
        from .slack import SlackConnector

        return SlackConnector(chat["slack"], token, http or urllib_http)
    from .discord import DiscordConnector

    return DiscordConnector(chat["discord"], token, http or urllib_http)
