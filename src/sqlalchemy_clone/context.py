"""The live CloneContext (passed to hooks/overrides) and the returned CloneResult."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ._types import DeferredColumn, IdKey, Instance, OriginalCopy, PrimaryKey
from .introspect import idkey_of, mapper_of, pk_of, pk_table

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from .spec import CloneSpec

# The concrete type of the cloned root: clone(x) -> CloneResult[type(x)], so result.root is x's type.
T = TypeVar("T")


class CloneContext:
    """Live state shared with every hook and field override during a clone.

    Exposes the ``{original -> copy}`` map so hooks can resolve related copies and the
    new ids embedded in text/JSON.
    """

    def __init__(self, spec: CloneSpec, session: Session | None) -> None:
        self.spec = spec
        self.session = session
        self.root: Instance = None
        self._pairs: list[OriginalCopy] = []
        self._by_idkey: dict[IdKey, Instance] = {}

    # -- internal bookkeeping (called by the engine) --
    def _record(self, original: Instance, copy: Instance, key: IdKey) -> None:
        self._pairs.append(OriginalCopy(original, copy))
        self._by_idkey[key] = copy

    # -- public, hook-facing API --
    @property
    def mapping(self) -> Mapping[Instance, Instance]:
        """A ``{original -> copy}`` mapping of every instance copied so far."""

        return dict(self._pairs)

    def new_of(self, original: Instance) -> Instance | None:
        """The copy made for ``original``, or None if it was not copied."""

        return self._by_idkey.get(idkey_of(original))

    def new_id(self, entity: type, old_pk: PrimaryKey) -> PrimaryKey | None:
        """The new primary key for the row of ``entity`` whose OLD pk was ``old_pk``,
        or None if that row was not copied.

        The bare-id lookup token-rewrite hooks need: tokens carry ids, not instances,
        and None deterministically means "out of set — leave the token untouched".
        ``entity`` may be any class mapped to the relevant table (a polymorphic base
        resolves its subclasses' rows).
        """

        copy = self._by_idkey.get((pk_table(mapper_of(entity)), (old_pk,)))
        return None if copy is None else pk_of(copy)

    def __iter__(self) -> Iterator[OriginalCopy]:
        return iter(self._pairs)


class CloneResult(Mapping[Any, Any], Generic[T]):
    """Read-only ``Mapping[original -> copy]`` plus the root and persistence info.

    Generic over the root's type: ``clone(project, ...)`` returns ``CloneResult[Project]``,
    so ``result.root`` is typed ``Project``.
    """

    def __init__(
        self,
        root: T,
        context: CloneContext,
        objects: list[Instance],
        deferred: list[DeferredColumn],
    ) -> None:
        self.root = root
        self.context = context
        self.objects = objects
        # FK columns the two-phase write deferred (nulled at insert) — for TRANSIENT replay.
        self.deferred = deferred

    def __getitem__(self, original: Instance) -> Instance:
        return self.context.mapping[original]

    def __iter__(self) -> Iterator[Instance]:
        return iter(self.context.mapping)

    def __len__(self) -> int:
        return len(self.context._pairs)

    def new_id(self, entity: type, old_pk: PrimaryKey) -> PrimaryKey | None:
        """The new primary key for ``entity``'s row whose OLD pk was ``old_pk``."""

        return self.context.new_id(entity, old_pk)
