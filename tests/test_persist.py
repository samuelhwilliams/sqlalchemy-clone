"""Persistence modes: TRANSIENT / ADD / FLUSH / COMMIT and transaction ownership."""

from __future__ import annotations

from sqlalchemy.orm import Session

from sqlalchemy_clone import Persist, clone

from .conftest import Graph
from .models import Project
from .specs import make_project_spec


def test_transient_adds_nothing(session: Session, graph: Graph) -> None:
    result = clone(graph.project, make_project_spec(), session=session, persist=Persist.TRANSIENT)
    assert result.root.id is not None  # PK minted in memory
    assert result.root not in session  # never added
    assert result.deferred  # nullable internal FKs reported for self-persist replay


def test_add_is_pending_not_flushed(session: Session, graph: Graph) -> None:
    result = clone(graph.project, make_project_spec(), session=session, persist=Persist.ADD)
    assert result.root in session.new  # pending, awaiting the caller's flush


def test_flush_is_visible_but_not_committed(session: Session, graph: Graph) -> None:
    result = clone(graph.project, make_project_spec(), session=session, persist=Persist.FLUSH)
    rid = result.root.id
    assert session.get(Project, rid) is not None  # written within the transaction
    session.rollback()
    assert session.get(Project, rid) is None  # clone() did not commit


def test_commit_is_durable(session: Session, graph: Graph) -> None:
    result = clone(graph.project, make_project_spec(), session=session, persist=Persist.COMMIT)
    rid = result.root.id
    session.rollback()  # nothing to undo
    assert session.get(Project, rid) is not None


def test_session_defaults_to_object_session(session: Session, graph: Graph) -> None:
    # graph.project is already attached to `session`; clone() should find it.
    result = clone(graph.project, make_project_spec(), persist=Persist.FLUSH)
    assert session.get(Project, result.root.id) is not None
