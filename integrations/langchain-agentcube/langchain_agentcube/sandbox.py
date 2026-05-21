# Copyright The Volcano Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""AgentCube Code Interpreter as a Deep Agents ``BaseSandbox`` backend."""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING

from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    GrepMatch,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox

from langchain_agentcube._paths import (
    execute_path_to_virtual,
    normalize_remote_path,
    rewrite_execute_command,
    virtual_to_execute_path,
)

if TYPE_CHECKING:
    from agentcube import CodeInterpreterClient

class AgentcubeSandbox(BaseSandbox):
    """Wraps :class:`~agentcube.CodeInterpreterClient` for ``create_deep_agent(..., backend=...)``."""

    def __init__(
        self,
        *,
        client: CodeInterpreterClient,
        default_timeout: int | None = 30 * 60,
        workspace_root: str | None = None,
    ) -> None:
        self._client = client
        self._default_timeout = default_timeout
        self._workspace_root = workspace_root
        self._workspace_root_resolving = False

    @property
    def id(self) -> str:
        sid = self._client.session_id
        return sid if sid else "agentcube-unknown"

    def _get_workspace_root(self) -> str | None:
        if self._workspace_root is not None:
            return self._workspace_root
        if self._workspace_root_resolving:
            return None
        self._workspace_root_resolving = True
        try:
            to = float(self._default_timeout) if self._default_timeout is not None else None
            r = self._client.execute_command_result("pwd", timeout=to)
            if int(r.get("exit_code", -1)) == 0:
                out = (r.get("stdout") or "").strip()
                if out:
                    self._workspace_root = out.splitlines()[-1].strip()
        finally:
            self._workspace_root_resolving = False
        return self._workspace_root

    def _execute_path(self, virtual_path: str) -> str:
        return virtual_to_execute_path(virtual_path, self._get_workspace_root())

    def _to_virtual_path(self, sandbox_path: str) -> str:
        return execute_path_to_virtual(sandbox_path, self._get_workspace_root())

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        eff = timeout if timeout is not None else self._default_timeout
        to = float(eff) if eff is not None else None
        adapted = rewrite_execute_command(command, self._get_workspace_root())
        r = self._client.execute_command_result(adapted, timeout=to)
        out = r.get("stdout") or ""
        stderr = (r.get("stderr") or "").strip()
        if stderr:
            out += f"\n<stderr>{stderr}</stderr>"
        return ExecuteResponse(
            output=out,
            exit_code=int(r.get("exit_code", -1)),
            truncated=False,
        )

    def ls(self, path: str) -> LsResult:
        result = super().ls(self._execute_path(path))
        entries: list[FileInfo] = []
        for entry in result.entries or []:
            entries.append(
                {
                    "path": self._to_virtual_path(entry["path"]),
                    "is_dir": entry["is_dir"],
                }
            )
        return LsResult(entries=entries)

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        disk = self._execute_path(file_path)
        result = super().read(disk, offset=offset, limit=limit)
        if result.error and disk != file_path:
            result = ReadResult(
                error=result.error.replace(f"'{disk}'", f"'{file_path}'").replace(
                    disk, file_path
                )
            )
        return result

    def write(self, file_path: str, content: str) -> WriteResult:
        disk = self._execute_path(file_path)
        preflight_error = self._write_preflight(disk)
        if preflight_error is not None:
            err = preflight_error.error or ""
            if err and disk != file_path:
                err = err.replace(repr(disk), repr(file_path)).replace(disk, file_path)
            return WriteResult(error=err)

        responses = self.upload_files([(file_path, content.encode("utf-8"))])
        if not responses:
            msg = (
                f"upload_files expected 1 result, got {len(responses)} "
                f"({type(responses)!r})"
            )
            raise AssertionError(msg)
        response = responses[0]
        if response.error:
            return WriteResult(
                error=f"Failed to write file '{file_path}': {response.error}"
            )
        # Deep Agents virtual path for follow-up read/ls; picod key is response.path (rel).
        return WriteResult(path=f"/{response.path}" if response.path else file_path)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002
    ) -> EditResult:
        disk = self._execute_path(file_path)
        result = super().edit(disk, old_string, new_string, replace_all=replace_all)
        if result.path and result.path == disk:
            return EditResult(path=file_path, occurrences=result.occurrences)
        if result.error and disk != file_path:
            return EditResult(
                error=result.error.replace(f"'{disk}'", f"'{file_path}'").replace(
                    disk, file_path
                )
            )
        return result

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        disk_path = self._execute_path(path) if path is not None else None
        result = super().grep(pattern, disk_path, glob=glob)
        if result.error:
            return result
        matches: list[GrepMatch] = []
        for match in result.matches or []:
            matches.append(
                {
                    "path": self._to_virtual_path(match["path"]),
                    "line": match["line"],
                    "text": match["text"],
                }
            )
        return GrepResult(matches=matches)

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        result = super().glob(pattern, self._execute_path(path))
        matches: list[FileInfo] = []
        for entry in result.matches or []:
            matches.append(
                {
                    "path": self._to_virtual_path(entry["path"]),
                    "is_dir": entry.get("is_dir", False),
                }
            )
        return GlobResult(matches=matches)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for path, content in files:
            rel = normalize_remote_path(path)
            if not rel:
                responses.append(FileUploadResponse(path=path, error="invalid_path"))
                continue
            tmp_path: str | None = None
            try:
                fd, tmp_path = tempfile.mkstemp(prefix="agentcube-upload-", suffix=".bin")
                with os.fdopen(fd, "wb") as f:
                    f.write(content)
                self._client.upload_file(tmp_path, rel)
                responses.append(FileUploadResponse(path=rel, error=None))
            except Exception as e:
                responses.append(FileUploadResponse(path=path, error=str(e)))
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            rel = normalize_remote_path(path)
            if not rel:
                responses.append(
                    FileDownloadResponse(path=path, content=None, error="invalid_path")
                )
                continue
            fd, tmp_path = tempfile.mkstemp(prefix="agentcube-dl-", suffix=".bin")
            os.close(fd)
            try:
                try:
                    self._client.download_file(rel, tmp_path)
                except Exception as e:  # noqa: BLE001
                    responses.append(
                        FileDownloadResponse(path=path, content=None, error=str(e))
                    )
                    continue
                with open(tmp_path, "rb") as f:
                    data = f.read()
                responses.append(
                    FileDownloadResponse(path=rel, content=data, error=None)
                )
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        return responses
