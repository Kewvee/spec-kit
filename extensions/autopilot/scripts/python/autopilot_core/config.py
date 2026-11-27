"""Autopilot configuration: defaults, loading and a small YAML-subset reader.

The config lives at ``.specify/extensions/autopilot/autopilot-config.yml``.
PyYAML is used when importable; otherwise a reader for the subset the
shipped template uses (nested mappings, block lists of scalars or mappings,
flow lists of scalars, quoted/unquoted scalars, comments) takes over, the same
approach the git extension's Python helpers take for ``git-config.yml``.
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

CONFIG_RELATIVE = Path(".specify") / "extensions" / "autopilot" / "autopilot-config.yml"
LOCAL_RELATIVE = CONFIG_RELATIVE.parent / "local-config.yml"
ENV_PREFIX = "SPECKIT_AUTOPILOT_"
# The epic workflow's build loop has a literal ceiling (do-while max_iterations).
WORKFLOW_ITERATION_CEILING = 25

DEFAULTS: dict[str, Any] = {
    "gate": {
        # Project commands that must all exit 0. Empty ``run`` entries are skipped;
        # preflight refuses to start when no check has a command.
        "checks": [],
        "check_timeout_seconds": 1800,
        "require_tasks_complete": True,
        "require_review": True,
        "acceptance": {
            "required": True,
            "test_globs": [
                "tests/**",
                "test/**",
                "spec/**",
                "**/__tests__/**",
                "**/*_test.*",
                "**/*.test.*",
                "**/*.spec.*",
                "**/test_*.*",
            ],
            # Optional per-scenario command; ``{tag}`` and ``{tag_underscore}``
            # are replaced with the scenario ID (always [A-Za-z0-9-]).
            "scenario_command": "",
        },
    },
    "pipe": {
        "max_iterations": 5,
        "stuck_after": 2,
        "timeout_minutes": 120,
        "max_attempts": 1,
        # Questions one developer may raise before the epic counts as under-specified.
        "max_questions": 8,
    },
    "run": {
        "max_parallel": 2,
        "timeout_minutes": 480,
        "max_agent_calls": 200,
        "base_branch": "",
        # ``{run}`` is replaced by the run ID, so every planned run gets fresh
        # branches and worktrees and never reuses an earlier run's work.
        "integration_branch": "autopilot/{run}/integration",
        "branch_prefix": "autopilot/{run}/",
        "worktree_root": "",
        "specify_command": "specify",
        # Shell command run once in every new worktree (install dependencies).
        "setup": "",
        "integration": "",
        "reviewer_model": "",
        "lead_model": "",
        "sync_paths": [
            ".specify",
            ".claude",
            ".github/agents",
            ".github/prompts",
            ".github/skills",
            ".gemini",
            ".codex",
            ".opencode",
            ".agents",
            ".cursor",
            ".windsurf",
            ".kiro",
        ],
    },
}


class ConfigError(ValueError):
    """Raised when the config file cannot be read or has the wrong shape."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(project_root: Path, path: Path | None = None) -> dict[str, Any]:
    """The effective configuration.

    Precedence, lowest first (the extension configuration contract):
    built-in defaults, ``autopilot-config.yml``, ``local-config.yml``, then
    ``SPECKIT_AUTOPILOT_<SECTION>_<KEY>`` environment variables. A pipe launched
    by the scheduler instead reads the run's snapshot named by
    ``AUTOPILOT_CONFIG``, so it always uses the conductor's current settings.
    """
    snapshot = os.environ.get("AUTOPILOT_CONFIG", "").strip()
    if path is None and snapshot:
        snapshot_path = Path(snapshot)
        try:
            data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"run config snapshot {snapshot_path} is unreadable: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{snapshot_path}: top level must be a mapping")
        config = _deep_merge(DEFAULTS, data)
        _validate(config, snapshot_path)
        return config
    config_path = path or (project_root / CONFIG_RELATIVE)
    config = copy.deepcopy(DEFAULTS)
    layers = [config_path] if path is not None else [config_path, project_root / LOCAL_RELATIVE]
    for layer in layers:
        if not layer.is_file():
            continue
        data = parse_yaml(layer.read_text(encoding="utf-8"))
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f"{layer}: top level must be a mapping")
        config = _deep_merge(config, data)
    if path is None:
        config = _deep_merge(config, env_overrides(os.environ))
    _validate(config, config_path)
    return config


def _leaf_paths(tree: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = []
    for key, value in tree.items():
        if isinstance(value, dict):
            paths += _leaf_paths(value, prefix + (key,))
        else:
            paths.append(prefix + (key,))
    return paths


def _env_value(raw: str) -> Any:
    text = raw.strip()
    if text[:1] in "[{":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return raw
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    return raw


def env_overrides(environ: Any) -> dict[str, Any]:
    """``SPECKIT_AUTOPILOT_PIPE_MAX_ITERATIONS=8`` -> ``{"pipe": {"max_iterations": 8}}``.

    Names are matched against the known keys, so keys that contain underscores
    work. Lists and check entries take JSON (``'["tests/**"]'``).
    """
    overrides: dict[str, Any] = {}
    for leaf in _leaf_paths(DEFAULTS):
        name = ENV_PREFIX + "_".join(part.upper() for part in leaf)
        if name not in environ:
            continue
        current = overrides
        for part in leaf[:-1]:
            current = current.setdefault(part, {})
        current[leaf[-1]] = _env_value(environ[name])
    return overrides


def write_snapshot(config: dict[str, Any], path: Path) -> Path:
    """Freeze the effective config for pipes (see ``AUTOPILOT_CONFIG``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _validate(config: dict[str, Any], path: Path) -> None:
    checks = config["gate"]["checks"]
    if not isinstance(checks, list):
        raise ConfigError(f"{path}: gate.checks must be a list")
    for index, check in enumerate(checks):
        if not isinstance(check, dict) or not isinstance(check.get("name"), str):
            raise ConfigError(f"{path}: gate.checks[{index}] needs a string 'name'")
        if not isinstance(check.get("run", ""), str):
            raise ConfigError(f"{path}: gate.checks[{index}].run must be a string")
    for section, key in (
        ("pipe", "max_iterations"),
        ("pipe", "stuck_after"),
        ("pipe", "timeout_minutes"),
        ("pipe", "max_attempts"),
        ("pipe", "max_questions"),
        ("run", "max_parallel"),
        ("run", "timeout_minutes"),
        ("run", "max_agent_calls"),
        ("gate", "check_timeout_seconds"),
    ):
        value = config[section][key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"{path}: {section}.{key} must be a positive integer")
    if config["pipe"]["max_iterations"] > WORKFLOW_ITERATION_CEILING:
        raise ConfigError(
            f"{path}: pipe.max_iterations must be at most {WORKFLOW_ITERATION_CEILING} "
            "(the autopilot-epic workflow's loop ceiling)"
        )
    for key in ("require_tasks_complete", "require_review"):
        if not isinstance(config["gate"][key], bool):
            raise ConfigError(f"{path}: gate.{key} must be true or false")
    acceptance = config["gate"]["acceptance"]
    if not isinstance(acceptance, dict):
        raise ConfigError(f"{path}: gate.acceptance must be a mapping")
    if not isinstance(acceptance.get("required"), bool):
        raise ConfigError(f"{path}: gate.acceptance.required must be true or false")
    globs = acceptance.get("test_globs")
    if not isinstance(globs, list) or not globs or not all(isinstance(g, str) and g.strip() for g in globs):
        raise ConfigError(f"{path}: gate.acceptance.test_globs must be a non-empty list of non-empty strings")
    if not isinstance(acceptance.get("scenario_command", ""), str):
        raise ConfigError(f"{path}: gate.acceptance.scenario_command must be a string")
    for key in ("base_branch", "integration_branch", "branch_prefix", "worktree_root", "specify_command",
                "setup", "integration", "reviewer_model", "lead_model"):
        if not isinstance(config["run"][key], str):
            raise ConfigError(f"{path}: run.{key} must be a string")
    if not config["run"]["specify_command"].strip():
        raise ConfigError(f"{path}: run.specify_command must not be empty")
    sync = config["run"]["sync_paths"]
    if not isinstance(sync, list) or not all(isinstance(p, str) and p.strip() for p in sync):
        raise ConfigError(f"{path}: run.sync_paths must be a list of non-empty strings")


def configured_checks(config: dict[str, Any]) -> list[dict[str, str]]:
    """Checks that have a non-empty command."""
    return [
        {"name": c["name"], "run": c.get("run", "")}
        for c in config["gate"]["checks"]
        if str(c.get("run", "")).strip()
    ]


# ── YAML subset ──────────────────────────────────────────────────────────────


def parse_yaml(text: str) -> Any:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return parse_yaml_subset(text)
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover - message passthrough
        raise ConfigError(f"invalid YAML: {exc}") from exc


_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_\-]*)\s*:(?:\s+(.*))?$")


def _strip_comment(line: str) -> str:
    """Drop a trailing ``# comment`` that is not inside quotes."""
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index].rstrip()
    return line.rstrip()


def _scalar(raw: str) -> Any:
    value = raw.strip()
    if value == "":
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in _split_flow(inner)]
    if value.startswith("{") and value == "{}":
        return {}
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        body = value[1:-1]
        if value[0] == '"':
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return body
        return body.replace("''", "'")
    lowered = value.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~", "none"):
        return None
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value)
    if re.fullmatch(r"[-+]?\d*\.\d+", value):
        return float(value)
    return value


def _split_flow(inner: str) -> list[str]:
    parts: list[str] = []
    current = ""
    quote = ""
    for char in inner:
        if quote:
            current += char
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
            current += char
        elif char == ",":
            parts.append(current)
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current)
    return parts


def parse_yaml_subset(text: str) -> Any:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise ConfigError("tabs are not allowed for indentation")
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        lines.append((len(stripped) - len(stripped.lstrip(" ")), stripped.strip()))
    if not lines:
        return None
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise ConfigError(f"unexpected content: {lines[index][1]!r}")
    return value


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if lines[index][1].startswith("- ") or lines[index][1] == "-":
        return _parse_list(lines, index, indent)
    return _parse_map(lines, index, indent)


def _parse_map(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict, int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ConfigError(f"unexpected indentation: {content!r}")
        match = _KEY_RE.match(content)
        if not match:
            raise ConfigError(f"expected 'key: value', got {content!r}")
        key, rest = match.group(1), match.group(2)
        index += 1
        if rest is None or rest == "":
            if index < len(lines) and (
                lines[index][0] > indent
                or (lines[index][0] == indent and lines[index][1].startswith("-"))
            ):
                child_indent = lines[index][0]
                result[key], index = _parse_block(lines, index, child_indent)
            else:
                result[key] = None
        else:
            result[key] = _scalar(rest)
    return result, index


def _parse_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list, int]:
    result: list[Any] = []
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent < indent or not (content.startswith("- ") or content == "-"):
            break
        if line_indent > indent:
            raise ConfigError(f"unexpected indentation: {content!r}")
        item = content[1:].strip()
        index += 1
        if not item:
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_block(lines, index, lines[index][0])
                result.append(value)
            else:
                result.append(None)
            continue
        if _KEY_RE.match(item):
            # "- key: value" starts an inline mapping; continuation keys are
            # indented to the column after "- ".
            item_indent = indent + 2
            sub_lines = [(item_indent, item)]
            while index < len(lines) and lines[index][0] >= item_indent:
                sub_lines.append(lines[index])
                index += 1
            value, consumed = _parse_map(sub_lines, 0, item_indent)
            if consumed != len(sub_lines):
                raise ConfigError(f"unexpected content in list item: {sub_lines[consumed][1]!r}")
            result.append(value)
        else:
            result.append(_scalar(item))
    return result, index
