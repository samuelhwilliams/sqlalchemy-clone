"""Spec validation, FK actions, ColumnRef equivalence, and the loud-failure modes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from sqlalchemy_clone import (
    KEEP,
    NULLOUT,
    REMAP,
    CloneSpec,
    Entity,
    ExternalDependencyError,
    Persist,
    PrimaryKeyMintError,
    SpecValidationError,
    UnbreakableCycleError,
    UnconfiguredEntityError,
    clone,
)

from .conftest import Graph
from .models import Chain, Counter, Link, Milestone, Project, Task
from .specs import make_project_spec


# -- validate() ---------------------------------------------------------------
def test_viewonly_rejected_in_follow() -> None:
    spec = CloneSpec(entities={Milestone: Entity(follow=[Milestone.all_tasks])})
    with pytest.raises(SpecValidationError, match="viewonly"):
        spec.validate()


def test_viewonly_allowed_in_exclude() -> None:
    spec = CloneSpec(
        entities={Milestone: Entity(follow=[Milestone.tasks], exclude=[Milestone.all_tasks])}
    )
    spec.validate()  # does not raise


def test_unknown_relationship_rejected() -> None:
    spec = CloneSpec(entities={Project: Entity(follow=["nope"])})
    with pytest.raises(SpecValidationError, match="nope"):
        spec.validate()


def test_unknown_field_key_rejected() -> None:
    spec = CloneSpec(entities={Project: Entity(fields={"nope": 1})})
    with pytest.raises(SpecValidationError, match="not a mapped column"):
        spec.validate()


def test_fk_key_must_be_a_foreign_key_column() -> None:
    spec = CloneSpec(entities={Project: Entity(fk={Project.name: KEEP})})
    with pytest.raises(SpecValidationError, match="foreign-key"):
        spec.validate()


def test_strict_requires_every_relationship_handled() -> None:
    # Project also has owner, pinned_task, datasets — not covered here.
    spec = CloneSpec(strict=True, entities={Project: Entity(follow=[Project.milestones])})
    with pytest.raises(SpecValidationError, match="strict"):
        spec.validate()


# -- on_unconfigured ----------------------------------------------------------
def test_on_unconfigured_error(session: Session, graph: Graph) -> None:
    spec = CloneSpec(
        on_unconfigured="error", entities={Project: Entity(follow=[Project.milestones])}
    )
    with pytest.raises(UnconfiguredEntityError, match="Milestone"):
        clone(graph.project, spec, session=session, persist=Persist.TRANSIENT)


# -- FK actions ---------------------------------------------------------------
def test_fk_nullout(session: Session, graph: Graph) -> None:
    spec = CloneSpec(
        entities={
            Project: Entity(follow=[Project.milestones]),
            Milestone: Entity(follow=[Milestone.tasks]),
            Task: Entity(follow=[Task.subtasks, Task.refs]),
            Link: Entity(fk={Link.depends_on_task_id: NULLOUT}),
        }
    )
    result = clone(graph.project, spec, session=session, persist=Persist.TRANSIENT)
    links = [c for o, c in result.context if isinstance(c, Link)]
    assert links
    assert all(link.depends_on_task_id is None for link in links)


def test_on_external_fk_error(session: Session, graph: Graph) -> None:
    # Project.owner_id points at an Account that is not copied -> AUTO + error raises.
    spec = make_project_spec(on_external_fk="error")
    with pytest.raises(ExternalDependencyError, match="owner_id"):
        clone(graph.project, spec, session=session, persist=Persist.TRANSIENT)


def test_remap_action_requires_in_set_referent(session: Session, graph: Graph) -> None:
    # Force REMAP on the external owner_id; its referent (Account) is not copied -> raise.
    spec = CloneSpec(entities={Project: Entity(fk={Project.owner_id: REMAP})})
    with pytest.raises(ExternalDependencyError):
        clone(graph.project, spec, session=session, persist=Persist.TRANSIENT)


# -- ColumnRef: strings and attributes are equivalent -------------------------
def test_columnref_string_and_attribute_equivalent(session: Session, graph: Graph) -> None:
    by_string = CloneSpec(entities={Project: Entity(fields={"name": "X", "slug": "y"})})
    by_attr = CloneSpec(entities={Project: Entity(fields={Project.name: "X", Project.slug: "y"})})
    r1 = clone(graph.project, by_string, session=session, persist=Persist.TRANSIENT)
    r2 = clone(graph.project, by_attr, session=session, persist=Persist.TRANSIENT)
    assert r1.root.name == r2.root.name == "X"
    assert r1.root.slug == r2.root.slug == "y"


# -- primary-key strategies ---------------------------------------------------
def test_server_generated_pk_raises(session: Session) -> None:
    counter = Counter(label="x")
    session.add(counter)
    session.flush()
    spec = CloneSpec(entities={Counter: Entity()})
    with pytest.raises(PrimaryKeyMintError):
        clone(counter, spec, session=session, persist=Persist.TRANSIENT)


def test_mint_pk_escape_hatch(session: Session) -> None:
    counter = Counter(label="x")
    session.add(counter)
    session.flush()
    spec = CloneSpec(mint_pk=lambda col, original: 9999, entities={Counter: Entity()})
    result = clone(counter, spec, session=session, persist=Persist.FLUSH)
    assert result.root.id == 9999
    assert result.root.label == "x"


# -- unbreakable cycle --------------------------------------------------------
def test_not_null_self_ref_is_unbreakable(session: Session) -> None:
    cid = uuid.uuid4()
    chain = Chain(id=cid, label="head", next_id=cid)  # NOT NULL self-reference
    session.add(chain)
    session.flush()
    spec = CloneSpec(entities={Chain: Entity()})
    with pytest.raises(UnbreakableCycleError):
        clone(chain, spec, session=session, persist=Persist.FLUSH)
