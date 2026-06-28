"""SQLAlchemy introspection helpers — the only module coupled to SA internals."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import ColumnDefault
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapper
from sqlalchemy.sql.elements import ColumnElement, TextClause

from ._types import IdKey, Instance, MappedColumn, PrimaryKey, Selectable
from .errors import PrimaryKeyMintError

if TYPE_CHECKING:
    from .spec import CloneSpec


def mapper_of(obj_or_cls: Any) -> Mapper:
    """The Mapper for an instance or a mapped class."""

    insp = sa_inspect(obj_or_cls)
    return insp if isinstance(insp, Mapper) else insp.mapper


def pk_table(mapper: Mapper) -> Selectable:
    """The table that holds this mapper's rows (its local table)."""

    return mapper.local_table


def idkey_of(instance: Instance) -> IdKey:
    """The identity-map key for an existing (original) instance."""

    m = mapper_of(instance)
    return (m.local_table, m.primary_key_from_instance(instance))


def pk_of(instance: Instance) -> PrimaryKey:
    """The primary key of an instance: a scalar for single-column PKs, else a tuple."""

    m = mapper_of(instance)
    pk = m.primary_key_from_instance(instance)
    return pk[0] if len(pk) == 1 else pk


def table_columns(mapper: Mapper) -> Iterator[MappedColumn]:
    """The mapper's persisted columns, as ``Column`` objects.

    ``mapper.local_table`` is a ``FromClause`` whose ``.c`` is typed ``KeyedColumnElement``,
    but at runtime these are the table's ``Column`` objects. The single cast here gives every
    caller the full ``Column`` attribute surface (``.nullable``, ``.foreign_keys``, ``.table``…)
    instead of an opaque element type.
    """

    return cast("Iterator[MappedColumn]", iter(mapper.local_table.c))


def pk_columns(mapper: Mapper) -> tuple[MappedColumn, ...]:
    """The mapper's primary-key columns (``mapper.primary_key`` is typed broadly as
    ``ColumnElement``; its members are ``Column`` objects)."""

    return cast("tuple[MappedColumn, ...]", mapper.primary_key)


def key_of(ref: Any) -> str:
    """Normalise a string name or an InstrumentedAttribute to its attribute key."""

    if isinstance(ref, str):
        return ref
    return ref.key


def fk_attr_keys(mapper: Mapper) -> set[str]:
    """Attribute keys of this mapper whose column carries a foreign key."""

    out: set[str] = set()
    for col in table_columns(mapper):
        if col.foreign_keys:
            prop = _prop_for(mapper, col)
            if prop is not None:
                out.add(prop)
    return out


def _prop_for(mapper: Mapper, col: MappedColumn) -> str | None:
    """The attribute key mapped to ``col``, or None if the column is unmapped."""

    try:
        return mapper.get_property_by_column(col).key
    except Exception:
        return None


def is_reset_column(mapper: Mapper, col: MappedColumn, spec: CloneSpec) -> bool:
    """Whether a column should be reset (left unset so the DB re-applies its default)
    rather than copied.

    Resets primary keys and SQL-function defaults (``func.now()`` timestamps,
    ``onupdate``/``server_onupdate``) — but never the polymorphic discriminator, and
    never a literal default such as ``server_default="{}"``.
    """

    if mapper.polymorphic_on is not None and col is mapper.polymorphic_on:
        return False
    if spec.reset_column is not None:
        return bool(spec.reset_column(col))
    sd = col.server_default
    return bool(
        col.primary_key
        or col.server_onupdate is not None
        or (col.onupdate is not None and not getattr(col.onupdate, "is_scalar", False))
        or (sd is not None and isinstance(getattr(sd, "arg", None), (ColumnElement, TextClause)))
    )


def mint_pks(mapper: Mapper, original: Instance, spec: CloneSpec) -> dict[str, PrimaryKey]:
    """Mint fresh primary-key values for a copy.

    Uses ``spec.mint_pk`` if given, else invokes each PK column's client-side default
    (a callable like ``uuid.uuid4`` or a scalar). Raises ``PrimaryKeyMintError`` for a
    primary key with no client-side default (e.g. a server-generated one), which the
    caller resolves by supplying ``spec.mint_pk``.
    """

    out: dict[str, PrimaryKey] = {}
    for col in pk_columns(mapper):
        prop = _prop_for(mapper, col)
        key: str = prop if prop is not None else col.key
        if spec.mint_pk is not None:
            out[key] = spec.mint_pk(col, original)
            continue
        default = col.default
        if not isinstance(default, ColumnDefault):  # None, Sequence, Identity, server-side, ...
            raise PrimaryKeyMintError(
                f"{mapper.class_.__name__}.{key}: primary key has no client-side default; "
                f"supply CloneSpec.mint_pk to provide one"
            )
        if default.is_scalar:
            out[key] = default.arg
        elif default.is_callable:
            out[key] = default.arg(None)
        else:
            raise PrimaryKeyMintError(
                f"{mapper.class_.__name__}.{key}: primary key default is a SQL expression, not a "
                f"client-side value; supply CloneSpec.mint_pk"
            )
    return out
