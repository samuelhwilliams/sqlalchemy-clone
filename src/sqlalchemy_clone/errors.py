"""Exception hierarchy for sqlalchemy-clone."""

from __future__ import annotations


class CloneError(Exception):
    """Base class for every error raised by sqlalchemy-clone."""


class SpecValidationError(CloneError):
    """A CloneSpec references an unknown relationship/column, a viewonly/secondary
    edge was placed in ``follow``, or ``strict`` found an unhandled relationship."""


class UnconfiguredEntityError(CloneError):
    """Traversal reached a class with no ``Entity`` while ``on_unconfigured='error'``."""


class UnbreakableCycleError(CloneError):
    """A NOT NULL foreign key must be deferred to break an insert cycle, which is
    impossible — the graph cannot be inserted on an immediate-check backend."""


class PrimaryKeyMintError(CloneError):
    """A primary key cannot be minted because the column has no usable client-side default
    (e.g. it is server-generated). Supply ``CloneSpec.mint_pk`` to provide the value."""


class ExternalDependencyError(CloneError):
    """An ``AUTO`` foreign key resolved to a referent outside the copy set while
    ``on_external_fk='error'``, or an assertive ``REMAP`` target was not copied."""
