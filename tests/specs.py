"""Reusable CloneSpec + hooks for the project-tracker graph used across tests."""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from sqlalchemy_clone import CloneContext, CloneSpec, Entity, Follow

from .models import Dataset, Milestone, Note, Project, Task

_T = re.compile(r"t_([0-9a-f]{32})")
_D = re.compile(r"d_([0-9a-f]{32})")


def rewrite_tokens(old: Any, new: Note, ctx: CloneContext) -> None:
    """after_remap hook: rewrite embedded t_<hex> (task) / d_<hex> (dataset) tokens to
    the new ids. Out-of-set tokens are left untouched (ctx.new_id -> None)."""

    def t(m: re.Match[str]) -> str:
        nid = ctx.new_id(Task, uuid.UUID(m.group(1)))
        return f"t_{nid.hex}" if nid is not None else m.group(0)

    def d(m: re.Match[str]) -> str:
        nid = ctx.new_id(Dataset, uuid.UUID(m.group(1)))
        return f"d_{nid.hex}" if nid is not None else m.group(0)

    new.body = _T.sub(t, new.body)
    new.detail = _D.sub(d, _T.sub(t, new.detail))


def make_project_spec(
    *,
    new_name: str = "Copy",
    new_slug: str = "copy",
    on_external_fk: Literal["keep", "null", "error"] = "keep",
    calls: dict[str, list[Any]] | None = None,
    s3: list[Any] | None = None,
) -> CloneSpec:
    """The canonical plan: copy a Project with its milestones/tasks/notes/links/datasets,
    rename it, rewrite note tokens, push managed datasets to "S3" after commit, and leave
    audit logs / external rows / viewonly edges behind."""

    calls = {} if calls is None else calls
    s3_list = [] if s3 is None else s3

    def recorder(phase: str) -> Any:
        def hook(old: Any, new: Any, ctx: CloneContext) -> None:
            calls.setdefault(phase, []).append(new)

        return hook

    def note_after_remap(old: Any, new: Note, ctx: CloneContext) -> None:
        rewrite_tokens(old, new, ctx)
        calls.setdefault("after_remap", []).append(new)

    def dataset_after_commit(old: Dataset, new: Dataset, ctx: CloneContext) -> None:
        if new.kind == "managed":
            s3_list.append(new.id)
        calls.setdefault("after_commit", []).append(new)

    return CloneSpec(
        on_external_fk=on_external_fk,
        entities={
            Project: Entity(
                follow=[Project.milestones, Project.datasets],
                fields={
                    Project.name: lambda o, n, c: new_name,
                    Project.slug: lambda o, n, c: new_slug,
                },
                after_copy=recorder("after_copy"),
            ),
            Milestone: Entity(
                follow=[Milestone.tasks],
                exclude=[Milestone.all_tasks],  # viewonly
            ),
            Task: Entity(  # one Entity serves Chore + Bug via MRO
                follow=[Task.subtasks, Task.notes, Task.refs],
                exclude=[Task.audit_logs],
            ),
            Note: Entity(after_remap=note_after_remap),
            Dataset: Entity(
                follow=[Follow(Dataset.rows, when=lambda d: d.kind == "inline")],
                exclude=[Dataset.external_rows, Dataset.tasks],
                after_commit=dataset_after_commit,
            ),
            # Link and DatasetRow need no Entity: reached as leaves, FKs auto-remapped.
        },
    )
