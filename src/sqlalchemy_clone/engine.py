"""The clone engine: spec validation + the five-phase deep-copy algorithm.

Phase 0  validate          — check the plan against the mapped models
Phase A  discover/construct — walk follow edges, build copies, mint PKs, copy columns
Phase B  remap             — rewrite FK columns by the copy-set membership rule
Phase C  after_remap       — hooks run with the complete {original -> copy} map
Phase D  persist           — library-owned two-phase (null-then-UPDATE) write
"""

from __future__ import annotations

import copy as _copy
from collections import defaultdict
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING, TypeVar

from sqlalchemy.orm import object_session

from ._types import (
    DeferredColumn,
    Edge,
    FkColumn,
    InsertWave,
    Instance,
    MappedColumn,
    NodeId,
    RemappedFk,
)
from .context import CloneContext, CloneResult
from .errors import (
    CloneError,
    ExternalDependencyError,
    SpecValidationError,
    UnbreakableCycleError,
    UnconfiguredEntityError,
)
from .introspect import (
    fk_attr_keys,
    idkey_of,
    is_reset_column,
    key_of,
    mapper_of,
    mint_pks,
    pk_columns,
    pk_of,
    table_columns,
)
from .spec import CloneSpec, Entity, FkAction, Follow, Persist

if TYPE_CHECKING:
    from sqlalchemy.orm import Mapper, Session

T = TypeVar("T")

# Policy for a class reached during traversal but absent from CloneSpec.entities.
_LEAF = Entity()


def clone(
    instance: T,
    spec: CloneSpec,
    *,
    session: Session | None = None,
    persist: Persist = Persist.FLUSH,
) -> CloneResult[T]:
    """Deep-copy ``instance`` and the subgraph described by ``spec`` onto fresh PKs.

    Foreign keys are remapped automatically: an FK is rewritten to the new copy if its
    referent was itself copied, otherwise it is kept. Returns a ``CloneResult`` whose
    ``.root`` is the new instance and which is a read-only ``{original -> copy}`` map.

    ``session`` defaults to ``object_session(instance)``. ``persist`` controls how far
    the new graph is taken toward the database (see ``Persist``).
    """

    validate_spec(spec)
    ctx = CloneContext(spec, session)
    run = _Run(spec=spec, ctx=ctx)

    new_root = _discover(run, instance)  # Phase A
    ctx.root = new_root
    _remap(run)  # Phase B
    _after_remap(run)  # Phase C

    result: CloneResult[T] = CloneResult(new_root, ctx, run.objects, [])
    target = session if session is not None else object_session(instance)
    _persist(run, target, persist, result)  # Phase D
    return result


# ---------------------------------------------------------------------------
# Phase 0 — validate
# ---------------------------------------------------------------------------
def validate_spec(spec: CloneSpec) -> None:
    """Check the plan against the mapped models; raise ``SpecValidationError``."""

    for cls, entity in spec.entities.items():
        mapper = mapper_of(cls)
        rels = mapper.relationships
        followed: set[str] = set()
        for f in entity.follow:
            rel = f.rel if isinstance(f, Follow) else f
            name = key_of(rel)
            if name not in rels:
                raise SpecValidationError(f"{cls.__name__}.follow: {name!r} is not a relationship")
            prop = rels[name]
            if prop.viewonly:
                raise SpecValidationError(
                    f"{cls.__name__}.follow: {name!r} is viewonly; it cannot be a copy edge "
                    f"(put it in exclude if you need to satisfy strict)"
                )
            if prop.secondary is not None:
                raise SpecValidationError(
                    f"{cls.__name__}.follow: {name!r} is a many-to-many (secondary); not a copy edge"
                )
            followed.add(name)

        excluded: set[str] = set()
        for e in entity.exclude:
            if isinstance(e, type):
                continue
            name = key_of(e)
            if name not in rels:
                raise SpecValidationError(f"{cls.__name__}.exclude: {name!r} is not a relationship")
            excluded.add(name)

        column_keys = {attr.key for attr in mapper.column_attrs}
        for ref in entity.fields:
            name = key_of(ref)
            if name not in column_keys:
                raise SpecValidationError(f"{cls.__name__}.fields: {name!r} is not a mapped column")

        fk_keys = fk_attr_keys(mapper)
        for ref in entity.fk:
            name = key_of(ref)
            if name not in fk_keys:
                raise SpecValidationError(
                    f"{cls.__name__}.fk: {name!r} is not a foreign-key column"
                )

        if spec.strict:
            for name in rels.keys():
                if name not in followed and name not in excluded:
                    raise SpecValidationError(
                        f"{cls.__name__}: relationship {name!r} is neither followed nor "
                        f"excluded (strict=True)"
                    )


# ---------------------------------------------------------------------------
# internal run state
# ---------------------------------------------------------------------------
@dataclass
class _Copy:
    original: Instance
    copy: Instance
    entity: Entity
    mapper: Mapper
    pinned: set[str] = field(default_factory=set)


@dataclass
class _Run:
    spec: CloneSpec
    ctx: CloneContext
    objects: list[Instance] = field(default_factory=list)
    copies: list[_Copy] = field(default_factory=list)
    internal_fk: list[RemappedFk] = field(default_factory=list)


def _resolve_entity(spec: CloneSpec, cls: type) -> Entity | None:
    for klass in cls.__mro__:
        if klass in spec.entities:
            return spec.entities[klass]
    return None


# ---------------------------------------------------------------------------
# Phase A — discover & construct
# ---------------------------------------------------------------------------
def _discover(run: _Run, instance: Instance) -> Instance:
    spec = run.spec
    ctx = run.ctx

    def visit(original: Instance) -> Instance:
        key = idkey_of(original)
        existing = ctx._by_idkey.get(key)
        if existing is not None:
            return existing

        cls = type(original)
        entity = _resolve_entity(spec, cls)
        if entity is None:
            if spec.on_unconfigured == "error":
                raise UnconfiguredEntityError(
                    f"{cls.__name__} was reached during traversal but is not in CloneSpec.entities"
                )
            entity = _LEAF

        mapper = mapper_of(original)
        copy = mapper.class_manager.new_instance()  # bypasses __init__, keeps subclass

        for attr_key, value in mint_pks(mapper, original, spec).items():
            setattr(copy, attr_key, value)

        pk_cols = set(pk_columns(mapper))
        for col in table_columns(mapper):
            if col in pk_cols:
                continue
            prop = _attr_key(mapper, col)
            if prop is None or is_reset_column(mapper, col, spec):
                continue
            setattr(copy, prop, _copy.deepcopy(getattr(original, prop)))

        record = _Copy(original, copy, entity, mapper)
        for ref, override in entity.fields.items():
            attr_key = key_of(ref)
            value = override(original, copy, ctx) if callable(override) else override
            setattr(copy, attr_key, value)
            record.pinned.add(attr_key)

        ctx._record(original, copy, key)
        run.objects.append(copy)
        run.copies.append(record)

        if entity.after_copy is not None:
            entity.after_copy(original, copy, ctx)

        for f in entity.follow:
            rel_ref = f.rel if isinstance(f, Follow) else f
            if isinstance(f, Follow) and f.when is not None and not f.when(original):
                continue
            name = key_of(rel_ref)
            related = getattr(original, name)
            if related is None:
                continue
            if mapper.relationships[name].uselist:
                for child in list(related):
                    visit(child)
            else:
                visit(related)
        return copy

    return visit(instance)


# ---------------------------------------------------------------------------
# Phase B — remap FK columns (the core rule)
# ---------------------------------------------------------------------------
def _remap(run: _Run) -> None:
    spec = run.spec
    ctx = run.ctx
    for rec in run.copies:
        original, copy, mapper = rec.original, rec.copy, rec.mapper
        fk_actions = {key_of(k): v for k, v in rec.entity.fk.items()}
        for col in table_columns(mapper):
            if not col.foreign_keys:
                continue
            attr_key = _attr_key(mapper, col)
            if attr_key is None or attr_key in rec.pinned:
                continue
            action = fk_actions.get(attr_key, FkAction.AUTO)
            if action is FkAction.NULLOUT:
                setattr(copy, attr_key, None)
                continue
            if action is FkAction.KEEP:
                continue
            old_val = getattr(original, attr_key)
            if old_val is None:  # a NULL FK stays NULL; short-circuit before external logic
                continue
            ref_table = next(iter(col.foreign_keys)).column.table
            target = ctx._by_idkey.get((ref_table, (old_val,)))
            if action is FkAction.REMAP:
                if target is None:
                    raise ExternalDependencyError(
                        f"{mapper.class_.__name__}.{attr_key}: REMAP target is not in the copy set"
                    )
                setattr(copy, attr_key, pk_of(target))
                run.internal_fk.append(RemappedFk(copy, attr_key, col, target))
            elif target is not None:  # AUTO, internal referent -> remap
                setattr(copy, attr_key, pk_of(target))
                run.internal_fk.append(RemappedFk(copy, attr_key, col, target))
            elif spec.on_external_fk == "null":
                setattr(copy, attr_key, None)
            elif spec.on_external_fk == "error":
                raise ExternalDependencyError(
                    f"{mapper.class_.__name__}.{attr_key}: referent is outside the copy set "
                    f"(on_external_fk='error')"
                )
            # else "keep": leave the verbatim-copied original value


# ---------------------------------------------------------------------------
# Phase C — after_remap hooks
# ---------------------------------------------------------------------------
def _after_remap(run: _Run) -> None:
    for rec in run.copies:
        if rec.entity.after_remap is not None:
            rec.entity.after_remap(rec.original, rec.copy, run.ctx)


# ---------------------------------------------------------------------------
# Phase D — persist (library-owned two-phase write)
# ---------------------------------------------------------------------------
def _persist(run: _Run, session: Session | None, persist: Persist, result: CloneResult) -> None:
    if persist is Persist.TRANSIENT or persist is Persist.ADD:
        # Wire everything in memory; report the nullable internal FKs a self-persisting
        # caller may have to defer to break cycles (conservative superset).
        result.deferred = [
            DeferredColumn(fk.copy, fk.attr_key) for fk in run.internal_fk if fk.column.nullable
        ]
        if persist is Persist.ADD:
            if session is None:
                raise CloneError("clone(persist=ADD) needs a Session: pass session=")
            session.add_all(run.objects)
        return

    if session is None:
        raise CloneError(
            "clone(persist=FLUSH/COMMIT) needs a Session: pass session= or call on an "
            "instance already attached to one"
        )

    # Order the inserts ourselves: SQLAlchemy's unit of work sorts by declared relationships
    # and cannot order a raw-FK graph whose mappers form a cycle. We defer (null at insert,
    # UPDATE back afterwards) ONLY the FK columns that actually close a cycle, so conditional
    # CHECK constraints on incidental nullable FKs are not transiently violated.
    waves, deferred = _plan_inserts(run.internal_fk, run.objects)
    result.deferred = [DeferredColumn(fk.copy, fk.attr_key) for fk in deferred]

    stash = [(fk.copy, fk.attr_key, getattr(fk.copy, fk.attr_key)) for fk in deferred]
    for copy, attr_key, _value in stash:
        setattr(copy, attr_key, None)

    for wave in waves:  # parents before children; rows within a wave are independent
        session.add_all(wave)
        session.flush()

    for copy, attr_key, value in stash:  # restore the deferred FKs now every row exists
        setattr(copy, attr_key, value)
    if stash:
        session.flush()

    for rec in run.copies:
        if rec.entity.after_flush is not None:
            rec.entity.after_flush(rec.original, rec.copy, run.ctx)

    if persist is Persist.COMMIT:
        session.commit()
        for rec in run.copies:
            if rec.entity.after_commit is not None:
                rec.entity.after_commit(rec.original, rec.copy, run.ctx)


def _plan_inserts(
    internal_fk: list[RemappedFk],
    nodes: list[Instance],
) -> tuple[list[InsertWave], list[FkColumn]]:
    """Decide insert waves and the minimal set of FK columns to defer.

    Builds the dependency graph over internal FK columns (a copy must be inserted after every
    referent it points at) and breaks cycles by deferring *only* the fully-nullable FK edges
    that close them. Deferring nulls a column at insert, so cutting only true cycle edges avoids
    nulling an incidental nullable FK that a conditional CHECK constraint may require to be
    populated. Returns the insert waves and the deferred ``(copy, attr_key, column)`` triples; raises
    ``UnbreakableCycleError`` if a cycle has no fully-nullable edge to cut.

    Nodes are keyed by ``id()`` so models overriding ``__eq__``/``__hash__`` don't collide.
    """

    by_id: dict[NodeId, Instance] = {id(n): n for n in nodes}
    edge_fks: dict[Edge, list[FkColumn]] = defaultdict(list)
    deferred: list[FkColumn] = []

    for copy, attr_key, col, target in internal_fk:
        child_id, parent_id = id(copy), id(target)
        if child_id == parent_id:  # a row referencing itself: only nulling can break it
            if not col.nullable:
                raise UnbreakableCycleError(
                    f"{col.table.name}.{attr_key}: a NOT NULL self-referential foreign key "
                    f"cannot be deferred to break the insert cycle"
                )
            deferred.append(FkColumn(copy, attr_key, col))
            continue
        edge_fks[(child_id, parent_id)].append(FkColumn(copy, attr_key, col))

    active: set[Edge] = set(edge_fks)
    sorter: TopologicalSorter[NodeId]

    while True:
        sorter = TopologicalSorter()
        for node in nodes:
            sorter.add(id(node))
        for child_id, parent_id in active:
            sorter.add(child_id, parent_id)
        try:
            sorter.prepare()
            break
        except CycleError as exc:
            edge = _deferrable_edge(exc.args[1], active, edge_fks)
            if edge is None:
                raise UnbreakableCycleError(
                    "NOT NULL foreign keys form an insert cycle that cannot be broken"
                ) from exc
            deferred.extend(edge_fks[edge])
            active.discard(edge)

    waves: list[InsertWave] = []
    while sorter.is_active():
        ready = sorter.get_ready()
        waves.append([by_id[i] for i in ready])
        sorter.done(*ready)

    return waves, deferred


def _deferrable_edge(
    cycle: list[NodeId],
    active: set[Edge],
    edge_fks: dict[Edge, list[FkColumn]],
) -> Edge | None:
    """An edge on ``cycle`` whose FK columns are all nullable, so nulling them removes the
    edge and breaks the cycle. graphlib reports the node order; we accept the edge in either
    direction (whichever matches a real dependency)."""

    for i in range(len(cycle) - 1):
        a, b = cycle[i], cycle[i + 1]
        for edge in ((a, b), (b, a)):
            fks = edge_fks.get(edge)
            if edge in active and fks and all(fk.column.nullable for fk in fks):
                return edge
    return None


def _attr_key(mapper: Mapper, col: MappedColumn) -> str | None:
    try:
        return mapper.get_property_by_column(col).key
    except Exception:
        return None
