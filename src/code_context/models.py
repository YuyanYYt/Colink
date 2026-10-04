"""Versioned synchronization messages; contents are always UTF-8 source text."""

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from code_context.policy import MAX_FILES, content_problem, excluded_path, validate_path


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_project(project_id: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", project_id):
        raise ValueError("project_id must be a 1-64 character lowercase identifier")
    return project_id


class FileChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    op: Literal["upsert", "delete"]
    path: str
    content: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def check_path(cls, value: str) -> str:
        return validate_path(value)

    @model_validator(mode="after")
    def check_content(self) -> "FileChange":
        if self.op == "delete":
            if self.content is not None or self.sha256 is not None:
                raise ValueError("delete must not include content or hash")
            return self
        if self.content is None or self.sha256 is None:
            raise ValueError("upsert requires content and sha256")
        if excluded_path(self.path):
            raise ValueError("file is excluded by the mandatory policy")
        problem = content_problem(self.content)
        if problem:
            raise ValueError(problem)
        if content_hash(self.content) != self.sha256:
            raise ValueError("sha256 does not match content")
        return self


class SyncBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocol_version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    base_revision: int = Field(ge=0)
    mode: Literal["full", "delta"]
    changes: list[FileChange] = Field(max_length=MAX_FILES * 2)

    @model_validator(mode="after")
    def check_changes(self) -> "SyncBatch":
        paths = [change.path for change in self.changes]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate paths in a synchronization batch")
        if self.mode == "full" and any(change.op == "delete" for change in self.changes):
            raise ValueError("a full snapshot only contains upserts")
        return self
