"""The declarative copy plan: CloneSpec, Entity, Follow, and the FkAction/Persist enums."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy.orm import InstrumentedAttribute

from ._types import Instance, MappedColumn, PrimaryKey

if TYPE_CHECKING:
    from .context import CloneContext

# A relationship or column may be named by string OR by its mapped attribute
# (Project.milestones, Project.name, Task.parent_id). Both normalise to attr.key.
RelRef = str | InstrumentedAttribute
ColumnRef = str | InstrumentedAttribute

# (original, copy, ctx) -> value. Used for column overrides.
FieldFn = Callable[[Instance, Instance, "CloneContext"], Any]
# A literal value, or a FieldFn computing one.
Override = FieldFn | Any
# (parent_instance) -> bool. Decides whether to follow an edge for a given parent.
WhenFn = Callable[[Instance], bool]
# (original, copy, ctx) -> None. A lifecycle hook.
Hook = Callable[[Instance, Instance, "CloneContext"], None]


class FkAction(Enum):
    """Per-column override of the automatic foreign-key remap rule."""

    AUTO = "auto"  # default: remap if referent copied, else honour on_external_fk
    KEEP = "keep"  # force: leave the FK pointing at the original referent
    REMAP = "remap"  # assertive: referent MUST be in the copy set, else raise
    NULLOUT = "nullout"  # force the (nullable) FK column to NULL on the copy


AUTO = FkAction.AUTO
KEEP = FkAction.KEEP
REMAP = FkAction.REMAP
NULLOUT = FkAction.NULLOUT


class Persist(Enum):
    """How far ``clone()`` takes the new graph toward the database."""

    TRANSIENT = "transient"  # wire copies in memory; add nothing; no flush
    ADD = "add"  # session.add_all; no flush
    FLUSH = "flush"  # add_all + library-owned two-phase write (DEFAULT)
    COMMIT = "commit"  # add_all + two-phase write + commit + after_commit hooks


@dataclass(frozen=True, slots=True)
class Follow:
    """A relationship edge to deep-copy, optionally conditional on the PARENT.

    ``when`` is evaluated per discovered parent during traversal and receives the
    parent instance only (not the child).
    """

    rel: RelRef
    when: WhenFn | None = None


@dataclass(frozen=True, slots=True)
class Entity:
    """Per-class copy plan. Every field is optional.

    A class reached during traversal but absent from ``CloneSpec.entities`` is copied
    as a leaf (all columns copied, all FKs auto-remapped, no further traversal) unless
    ``CloneSpec.on_unconfigured == 'error'``.

    Policy is resolved by walking ``type(obj).__mro__``, so one Entity registered for a
    polymorphic base serves all single-table subclasses.
    """

    follow: Sequence[RelRef | Follow] = ()  # allow-list of edges to deep-copy
    exclude: Sequence[RelRef | type] = ()  # hard-stop edges/classes (guard rail)
    fields: Mapping[ColumnRef, Override] = field(default_factory=dict)  # column overrides
    fk: Mapping[ColumnRef, FkAction] = field(default_factory=dict)  # per-FK overrides
    after_copy: Hook | None = None
    after_remap: Hook | None = None
    after_flush: Hook | None = None
    after_commit: Hook | None = None  # fires only after a successful commit


@dataclass(frozen=True, slots=True)
class CloneSpec:
    """The entire copy plan, as data."""

    entities: Mapping[type, Entity] = field(default_factory=dict)

    on_unconfigured: Literal["leaf", "error"] = "leaf"
    on_external_fk: Literal["keep", "null", "error"] = "keep"  # AUTO action only
    strict: bool = False  # validate() asserts every relationship is followed XOR excluded

    # Provide primary-key values yourself instead of using each column's client-side default
    # — e.g. for server-generated, sequence, or composite keys, or a custom id scheme.
    # (col, original) -> new_pk.
    mint_pk: Callable[[MappedColumn, Instance], PrimaryKey] | None = None
    # Override the "reset this column; let the DB re-apply its default" predicate.
    reset_column: Callable[[MappedColumn], bool] | None = None

    def validate(self) -> None:
        """Eagerly check the plan against the mapped models. Called by ``clone()``.

        Resolves every follow/exclude entry to a real relationship and every fields/fk
        key to a real column (fk keys must be FK columns); rejects viewonly/secondary
        edges placed in ``follow`` (they are legal in ``exclude``); honours ``strict``.
        """

        from .engine import validate_spec

        validate_spec(self)
