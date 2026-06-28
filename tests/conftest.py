"""Pytest fixtures: a cross-backend engine, a session, and a sample object graph.

SQLite runs with ``PRAGMA foreign_keys=ON`` so foreign keys are enforced immediately —
the same regime as PostgreSQL — which is what makes the two-phase cycle-breaking write a
real test rather than a no-op. Pass ``--pg-url`` to also run the suite against Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import (
    Account,
    AuditLog,
    Base,
    Bug,
    Chore,
    Dataset,
    DatasetExternalRow,
    DatasetRow,
    Link,
    Milestone,
    Note,
    Project,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--pg-url", action="store", default=None, help="PostgreSQL URL to also test against")


def _backends(config: pytest.Config) -> list[str]:
    backends = ["sqlite"]
    if config.getoption("--pg-url"):
        backends.append("postgres")
    return backends


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "engine" in metafunc.fixturenames:
        metafunc.parametrize("engine", _backends(metafunc.config), indirect=True)


@pytest.fixture
def engine(request: pytest.FixtureRequest) -> Engine:
    if request.param == "sqlite":
        eng = create_engine("sqlite+pysqlite:///:memory:")

        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    else:
        eng = create_engine(request.config.getoption("--pg-url"))

    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s


@dataclass
class Graph:
    """A populated Project and handles to its parts, plus a snapshot of original ids."""

    account: Account
    project: Project
    milestone: Milestone
    chore: Chore  # top-level task
    bug: Bug  # top-level task (acts as a container)
    subtask: Chore  # nested under bug
    note: Note
    ds_inline: Dataset
    ds_managed: Dataset
    row0: DatasetRow
    link_task: Link  # cross-link via note + depends_on_task
    link_dataset: Link  # cross-link via dataset + dataset_row
    ids: dict[str, Any]


@pytest.fixture
def graph(session: Session) -> Graph:
    account = Account(name="External Owner")
    session.add(account)
    session.flush()

    project = Project(name="Orig", slug="orig", owner=account)
    ds_inline = Dataset(kind="inline", name="inline", project=project)
    ds_managed = Dataset(kind="managed", name="managed", project=project)
    row0 = DatasetRow(position=0, code="a", dataset=ds_inline)
    row1 = DatasetRow(position=1, code="b", dataset=ds_inline)
    # A row on the managed dataset that must NOT be copied (Follow(when=kind=="inline")).
    DatasetRow(position=0, code="m", dataset=ds_managed)
    DatasetExternalRow(external_id="ext-1", dataset=ds_managed)

    milestone = Milestone(title="M1", position=0, project=project)
    chore = Chore(title="T1", position=0, milestone=milestone)
    bug = Bug(title="G1", position=1, milestone=milestone, dataset=ds_inline)
    subtask = Chore(title="T2", position=0, parent=bug, milestone=milestone)

    session.add_all([project, ds_inline, ds_managed, row0, row1, milestone, chore, bug, subtask])
    session.flush()

    note = Note(
        body=f"see t_{chore.id.hex}",
        detail=f"ref t_{chore.id.hex} and d_{ds_inline.id.hex}.col",
        task=subtask,
        author=account,
    )
    session.add(note)
    session.flush()

    link_task = Link(task=subtask, note=note, depends_on_task=chore, label="cond")
    link_dataset = Link(
        task=subtask,
        depends_on_dataset=ds_inline,
        depends_on_dataset_row=row0,
        depends_on_field_name="col",
    )
    audit = AuditLog(message="created", task=chore)  # excluded from clone
    session.add_all([link_task, link_dataset, audit])
    project.pinned_task_id = chore.id  # nullable cyclic cross-ref
    session.flush()

    # Sanity: the things the clone must EXCLUDE really exist on the source.
    assert chore.audit_logs, "fixture should persist an audit log to exclude"
    assert ds_managed.external_rows, "fixture should persist an external row to exclude"
    assert len(ds_managed.rows) == 1, "fixture should persist a managed row to skip"

    ids = {
        "account": account.id,
        "project": project.id,
        "milestone": milestone.id,
        "chore": chore.id,
        "bug": bug.id,
        "subtask": subtask.id,
        "note": note.id,
        "ds_inline": ds_inline.id,
        "ds_managed": ds_managed.id,
        "row0": row0.id,
        "created_at": project.created_at,
    }
    return Graph(
        account=account,
        project=project,
        milestone=milestone,
        chore=chore,
        bug=bug,
        subtask=subtask,
        note=note,
        ds_inline=ds_inline,
        ds_managed=ds_managed,
        row0=row0,
        link_task=link_task,
        link_dataset=link_dataset,
        ids=ids,
    )
