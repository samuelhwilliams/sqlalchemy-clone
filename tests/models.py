"""Generic ORM models exercising every structurally-hard feature of a deep clone.

Plain project-tracker vocabulary, deliberately decoupled from any real consumer:

- polymorphic single-table inheritance  -> Task / Chore / Bug on ``kind``
- self-referential FK + ordering_list    -> Task.parent_id / Task.subtasks
- one-to-many nesting                     -> Project -> Milestone -> Task; Dataset -> DatasetRow
- a cross-link table with several FKs     -> Link (all pointing inside the copy set)
- an external FK to keep                  -> Project.owner_id / Note.author_id -> Account
- a nullable cyclic cross-reference       -> Project.pinned_task_id -> Task
- relationships to exclude                -> Task.audit_logs, Dataset.external_rows
- a viewonly relationship to auto-skip    -> Milestone.all_tasks
- client-side uuid PKs + server defaults  -> PkTimestamps
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, func
from sqlalchemy.ext.orderinglist import ordering_list
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class PkTimestamps:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class Account(PkTimestamps, Base):
    """External owner of work — never cloned."""

    __tablename__ = "account"
    name: Mapped[str]


class Project(PkTimestamps, Base):
    __tablename__ = "project"
    name: Mapped[str]
    slug: Mapped[str]
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("account.id"))  # external FK
    owner: Mapped[Account] = relationship()
    pinned_task_id: Mapped[uuid.UUID | None] = mapped_column(  # nullable cyclic cross-ref
        ForeignKey("task.id"), nullable=True
    )
    pinned_task: Mapped["Task | None"] = relationship(foreign_keys=[pinned_task_id])
    milestones: Mapped[list["Milestone"]] = relationship(
        back_populates="project",
        order_by="Milestone.position",
        collection_class=ordering_list("position"),
    )
    datasets: Mapped[list["Dataset"]] = relationship(back_populates="project")


class Milestone(PkTimestamps, Base):
    __tablename__ = "milestone"
    title: Mapped[str]
    position: Mapped[int]
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project.id"))
    project: Mapped[Project] = relationship(back_populates="milestones")
    all_tasks: Mapped[list["Task"]] = relationship(  # viewonly -> must auto-skip
        viewonly=True, order_by="Task.position"
    )
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="milestone",
        primaryjoin="and_(Task.milestone_id==Milestone.id, Task.parent_id.is_(None))",
        order_by="Task.position",
        collection_class=ordering_list("position"),
    )


class Task(PkTimestamps, Base):
    """Polymorphic single-table base. Subtasks via self-referential parent_id."""

    __tablename__ = "task"
    title: Mapped[str]
    position: Mapped[int]
    kind: Mapped[str] = mapped_column(String(20))
    milestone_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("milestone.id"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task.id"), nullable=True)
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dataset.id"), nullable=True)

    milestone: Mapped[Milestone] = relationship(
        back_populates="tasks",
        primaryjoin="and_(Task.milestone_id==Milestone.id, Task.parent_id.is_(None))",
    )
    parent: Mapped["Task | None"] = relationship(
        remote_side="Task.id", back_populates="subtasks"
    )
    subtasks: Mapped[list["Task"]] = relationship(
        back_populates="parent",
        order_by="Task.position",
        collection_class=ordering_list("position"),
    )
    dataset: Mapped["Dataset | None"] = relationship(
        back_populates="tasks", foreign_keys=[dataset_id]
    )
    notes: Mapped[list["Note"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="Note.created_at"
    )
    refs: Mapped[list["Link"]] = relationship(
        back_populates="task",
        foreign_keys="Link.task_id",
        cascade="all, delete-orphan",
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship(  # excluded from copy
        back_populates="task", cascade="all, delete-orphan"
    )
    depended_on_by: Mapped[list["Link"]] = relationship(
        back_populates="depends_on_task", foreign_keys="Link.depends_on_task_id"
    )

    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "task"}


class Chore(Task):
    __mapper_args__ = {"polymorphic_identity": "chore"}


class Bug(Task):
    __mapper_args__ = {"polymorphic_identity": "bug"}


class Note(PkTimestamps, Base):
    """Holds t_<hex> (task) and d_<hex> (dataset) tokens in ``detail`` to be rewritten."""

    __tablename__ = "note"
    body: Mapped[str]
    detail: Mapped[str]
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task.id"))
    task: Mapped[Task] = relationship(back_populates="notes")
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("account.id"))  # external FK
    author: Mapped[Account] = relationship()
    links: Mapped[list["Link"]] = relationship(
        back_populates="note", foreign_keys="Link.note_id", cascade="all, delete-orphan"
    )


class Dataset(PkTimestamps, Base):
    __tablename__ = "dataset"
    kind: Mapped[str] = mapped_column(String(20))  # "inline" | "managed"
    name: Mapped[str | None]
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project.id"))
    project: Mapped[Project | None] = relationship(back_populates="datasets")
    tasks: Mapped[list[Task]] = relationship(  # back-ref, never followed
        back_populates="dataset", foreign_keys="Task.dataset_id"
    )
    rows: Mapped[list["DatasetRow"]] = relationship(
        back_populates="dataset",
        order_by="DatasetRow.position",
        collection_class=ordering_list("position"),
    )
    external_rows: Mapped[list["DatasetExternalRow"]] = relationship(  # excluded for managed
        back_populates="dataset", cascade="all, delete-orphan"
    )

    # A conditional CHECK on a NULLABLE foreign key: a "managed" dataset requires a project_id.
    # project_id must therefore not be nulled at insert just because it is nullable — it is not
    # on an insert cycle, so the clone must keep its remapped value.
    __table_args__ = (
        CheckConstraint(
            "kind = 'inline' OR project_id IS NOT NULL", name="ck_dataset_managed_requires_project"
        ),
    )


class DatasetRow(PkTimestamps, Base):
    __tablename__ = "dataset_row"
    position: Mapped[int]
    code: Mapped[str]
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset.id"))
    dataset: Mapped[Dataset] = relationship(back_populates="rows")


class DatasetExternalRow(PkTimestamps, Base):
    __tablename__ = "dataset_external_row"
    external_id: Mapped[str]
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("dataset.id"))
    dataset: Mapped[Dataset] = relationship(back_populates="external_rows")


class AuditLog(PkTimestamps, Base):
    __tablename__ = "audit_log"
    message: Mapped[str]
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task.id"))
    task: Mapped[Task] = relationship(back_populates="audit_logs")


class Link(PkTimestamps, Base):
    """Cross-link table: every FK points inside the copy set and must be remapped."""

    __tablename__ = "link"
    label: Mapped[str | None]
    depends_on_field_name: Mapped[str | None]
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task.id"))
    task: Mapped[Task] = relationship(back_populates="refs", foreign_keys=[task_id])
    note_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("note.id"))
    note: Mapped["Note | None"] = relationship(back_populates="links", foreign_keys=[note_id])
    depends_on_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task.id"))
    depends_on_task: Mapped["Task | None"] = relationship(
        back_populates="depended_on_by", foreign_keys=[depends_on_task_id]
    )
    depends_on_dataset_row_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_row.id")
    )
    depends_on_dataset_row: Mapped["DatasetRow | None"] = relationship(
        foreign_keys=[depends_on_dataset_row_id]
    )
    depends_on_dataset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("dataset.id"))
    depends_on_dataset: Mapped["Dataset | None"] = relationship(
        foreign_keys=[depends_on_dataset_id]
    )


# A standalone model with a NOT NULL self-referential FK, used to prove the engine
# raises UnbreakableCycleError rather than emitting a raw IntegrityError.
class Chain(PkTimestamps, Base):
    __tablename__ = "chain"
    label: Mapped[str]
    # NOT NULL self-reference (the head points at itself) — an unbreakable insert cycle.
    next_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chain.id"))
    nexts: Mapped[list["Chain"]] = relationship(remote_side="Chain.id")


# A model with a server-generated (autoincrement) PK: it has no client-side default, so it
# proves PrimaryKeyMintError as well as the CloneSpec.mint_pk escape hatch.
class Counter(Base):
    __tablename__ = "counter"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    label: Mapped[str]
