"""sqlalchemy-clone — deep-copy a SQLAlchemy ORM instance and a chosen slice of its
graph onto fresh primary keys, with automatic foreign-key remapping.

Native SQLAlchemy 2.0+, cross-backend. The whole surface is ``clone()`` + the declarative
``CloneSpec``::

    from sqlalchemy_clone import clone, CloneSpec, Entity, Follow

    spec = CloneSpec(entities={
        Project: Entity(follow=[Project.milestones], fields={Project.name: "Copy"}),
        Milestone: Entity(follow=[Milestone.tasks]),
    })
    new_project = clone(project, spec, session=session).root
"""

from __future__ import annotations

from .context import CloneContext, CloneResult
from .engine import clone
from .errors import (
    CloneError,
    ExternalDependencyError,
    PrimaryKeyMintError,
    SpecValidationError,
    UnbreakableCycleError,
    UnconfiguredEntityError,
)
from .spec import (
    AUTO,
    KEEP,
    NULLOUT,
    REMAP,
    CloneSpec,
    Entity,
    FkAction,
    Follow,
    Persist,
)

__all__ = [
    "clone",
    "CloneSpec",
    "Entity",
    "Follow",
    "Persist",
    "FkAction",
    "AUTO",
    "KEEP",
    "REMAP",
    "NULLOUT",
    "CloneContext",
    "CloneResult",
    "CloneError",
    "SpecValidationError",
    "UnconfiguredEntityError",
    "UnbreakableCycleError",
    "PrimaryKeyMintError",
    "ExternalDependencyError",
]
