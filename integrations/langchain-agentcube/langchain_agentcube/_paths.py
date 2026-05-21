# Copyright The Volcano Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Map Deep Agents virtual paths (``/foo``) to AgentCube workspace / shell paths."""

from __future__ import annotations

import re

# Paths that must stay as real OS absolutes inside ``execute`` (not workspace virtual).
_SYSTEM_PATH_PREFIXES: tuple[str, ...] = (
    "/usr/",
    "/bin/",
    "/etc/",
    "/lib/",
    "/opt/",
    "/var/",
    "/tmp/",
    "/dev/",
    "/proc/",
    "/sys/",
    "/sbin/",
    "/run/",
    "/mnt/",
    "/media/",
    "/boot/",
)

# Tokens like /hello.py or /root/hello.py inside shell commands.
_EXECUTE_PATH_RE = re.compile(
    r"/(?:[a-zA-Z0-9._@+-]+/)*[a-zA-Z0-9._@+-]+"
)


def normalize_remote_path(path: str) -> str:
    """Picod / file API: workspace-relative (strip leading ``/``)."""
    return path.replace("\\", "/").strip().lstrip("/")


def virtual_to_execute_path(virtual: str, workspace_root: str | None = None) -> str:
    """Deep Agents virtual path -> path for ``BaseSandbox`` shell/Python (cwd = workspace)."""
    v = virtual.replace("\\", "/").strip()
    if not v or v == "/":
        return "."
    if workspace_root:
        w = workspace_root.rstrip("/")
        if v == w:
            return "."
        prefix = w + "/"
        if v.startswith(prefix):
            suffix = v[len(prefix) :]
            return suffix or "."
    rel = v.lstrip("/")
    return rel or "."


def execute_path_to_virtual(path: str, workspace_root: str | None = None) -> str:
    """Shell listing/grep path -> Deep Agents virtual path (leading ``/``)."""
    p = path.replace("\\", "/").strip()
    if p in (".", ""):
        return "/"
    if workspace_root:
        w = workspace_root.rstrip("/")
        if p == w:
            return "/"
        if p.startswith(w + "/"):
            suffix = p[len(w) + 1 :]
            return "/" + suffix if suffix else "/"
    if p.startswith("./"):
        p = p[2:]
    if not p.startswith("/"):
        return "/" + p
    return p


def rewrite_execute_command(command: str, workspace_root: str | None) -> str:
    """Rewrite virtual ``/file`` tokens to workspace-relative names for shell."""
    if not workspace_root:
        return command

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if any(token.startswith(prefix) for prefix in _SYSTEM_PATH_PREFIXES):
            return token
        w = workspace_root.rstrip("/")
        if token == w or token.startswith(w + "/"):
            if token == w:
                return "."
            return token[len(w) + 1 :] or "."
        return token.lstrip("/") or "."

    return _EXECUTE_PATH_RE.sub(_replace, command)
