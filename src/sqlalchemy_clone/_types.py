"""Internal semantic type aliases.

The engine threads a handful of small records and graph structures around. Spelling them
inline as ``tuple[Any, str, Any]`` / ``dict[tuple[int, int], ...]`` is unreadable, so they are
named here in one place. The ``Any``-based aliases stand in for SQLAlchemy objects that share
no useful supertype to annotate against; naming them still documents intent at each use site.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from sqlalchemy import Column, FromClause

# A mapped ORM instance — an original or its copy. There is no universal base class for
# mapped objects in a generic library (a consumer may use DeclarativeBase, the legacy
# declarative_base(), or imperative mapping — none shared), and this alias also types the
# public hook/override callables, where gradual ``Any`` lets a consumer write hooks with
# concrete model parameter types (``object`` would reject them by contravariance). The
# *root* of a clone is typed precisely instead, via ``CloneResult[T]``.
type Instance = Any

# Concrete SQLAlchemy types where they exist and carry the attributes we use:
type MappedColumn = Column[Any]  # carries .nullable/.foreign_keys/.table/.server_default/...
type Selectable = FromClause  # a mapper's persistence table (Mapper.local_table)

# A primary-key value is genuinely arbitrary (int, UUID, str, ...): Any is correct.
type PrimaryKey = Any

# --- identity map ------------------------------------------------------------
type PkTuple = tuple[Any, ...]  # the result of Mapper.primary_key_from_instance(...)
# Key for the {original -> copy} identity map: the row's table plus its old PK tuple.
# Keying by table (not class) unifies single-table polymorphic subclasses.
type IdKey = tuple[Selectable, PkTuple]


class OriginalCopy(NamedTuple):
    """A cloned pair, as yielded when iterating a CloneContext / CloneResult."""

    original: Instance
    copy: Instance


# --- foreign-key bookkeeping for the two-phase write -------------------------
class RemappedFk(NamedTuple):
    """An FK column on a copy that was rewritten to point at an in-set copy."""

    copy: Instance
    attr_key: str
    column: MappedColumn
    target: Instance


class FkColumn(NamedTuple):
    """An FK column considered for / chosen for deferral while breaking an insert cycle."""

    copy: Instance
    attr_key: str
    column: MappedColumn


class DeferredColumn(NamedTuple):
    """Public record on ``CloneResult.deferred``: an FK column the write deferred."""

    copy: Instance
    attr_key: str


# --- insert-planning graph (nodes keyed by id() of each copy) -----------------
type NodeId = int
type Edge = tuple[NodeId, NodeId]  # (child, parent): the child must be inserted after the parent
type InsertWave = list[Instance]  # independent rows that can be inserted together
