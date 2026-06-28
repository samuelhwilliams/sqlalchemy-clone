"""End-to-end behaviour of clone() over the project-tracker graph."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from sqlalchemy_clone import Persist, clone

from .conftest import Graph
from .models import Dataset, DatasetRow, Project, Task
from .specs import make_project_spec


@pytest.fixture
def cloned(session: Session, graph: Graph) -> SimpleNamespace:
    calls: dict[str, list] = {}
    s3: list = []
    spec = make_project_spec(calls=calls, s3=s3)
    result = clone(graph.project, spec, session=session, persist=Persist.COMMIT)
    new = session.get(Project, result.root.id)
    return SimpleNamespace(result=result, graph=graph, session=session, calls=calls, s3=s3, new=new)


def _bug(new_project: Project) -> Task:
    return next(t for t in new_project.milestones[0].tasks if t.title == "G1")


def _chore(new_project: Project) -> Task:
    return next(t for t in new_project.milestones[0].tasks if t.title == "T1")


def test_root_and_field_overrides(cloned: SimpleNamespace) -> None:
    new, g = cloned.new, cloned.graph
    assert new.id != g.ids["project"]
    assert new.name == "Copy"
    assert new.slug == "copy"
    assert new.owner_id == g.ids["account"]  # external FK kept


def test_structure_counts_and_ordering(cloned: SimpleNamespace) -> None:
    new = cloned.new
    assert len(new.milestones) == 1
    assert len(new.datasets) == 2
    assert [t.position for t in new.milestones[0].tasks] == [0, 1]


def test_polymorphic_identity_preserved(cloned: SimpleNamespace) -> None:
    top = cloned.new.milestones[0].tasks
    assert {t.title: type(t).__name__ for t in top} == {"T1": "Chore", "G1": "Bug"}
    bug = _bug(cloned.new)
    assert len(bug.subtasks) == 1
    assert type(bug.subtasks[0]).__name__ == "Chore"


def test_self_reference_remapped(cloned: SimpleNamespace) -> None:
    bug = _bug(cloned.new)
    assert bug.subtasks[0].parent_id == bug.id


def test_cyclic_cross_reference_remapped(cloned: SimpleNamespace) -> None:
    new_chore_id = cloned.result.new_id(Task, cloned.graph.ids["chore"])
    assert cloned.new.pinned_task_id == new_chore_id


def test_many_to_one_remapped(cloned: SimpleNamespace) -> None:
    bug = _bug(cloned.new)
    assert bug.dataset_id == cloned.result.new_id(Dataset, cloned.graph.ids["ds_inline"])


def test_conditional_follow(cloned: SimpleNamespace) -> None:
    by_kind = {d.kind: d for d in cloned.new.datasets}
    assert [r.code for r in by_kind["inline"].rows] == ["a", "b"]
    assert by_kind["managed"].rows == []  # skipped by Follow(when=kind=="inline")


def test_conditional_check_on_nullable_fk_not_deferred(cloned: SimpleNamespace) -> None:
    # Dataset has CHECK (kind='inline' OR project_id IS NOT NULL). project_id is nullable
    # but, since it is not on an insert cycle, it must keep its value at INSERT rather than
    # being nulled by the cycle-breaking pass — otherwise the managed copy violates the CHECK.
    by_kind = {d.kind: d for d in cloned.new.datasets}
    assert by_kind["managed"].project_id == cloned.new.id


def test_excluded_relationships_not_copied(cloned: SimpleNamespace) -> None:
    assert _chore(cloned.new).audit_logs == []
    by_kind = {d.kind: d for d in cloned.new.datasets}
    assert by_kind["managed"].external_rows == []


def test_external_fk_kept_on_note(cloned: SimpleNamespace) -> None:
    note = _bug(cloned.new).subtasks[0].notes[0]
    assert note.author_id == cloned.graph.ids["account"]


def test_crosslink_table_fully_remapped(cloned: SimpleNamespace) -> None:
    g = cloned.graph
    sub = _bug(cloned.new).subtasks[0]
    links = sub.refs
    assert len(links) == 2
    by_task = next(link for link in links if link.depends_on_task_id is not None)
    by_dataset = next(link for link in links if link.depends_on_dataset_id is not None)

    assert by_task.task_id == sub.id
    assert by_task.note_id == sub.notes[0].id
    assert by_task.depends_on_task_id == cloned.result.new_id(Task, g.ids["chore"])

    assert by_dataset.depends_on_dataset_id == cloned.result.new_id(Dataset, g.ids["ds_inline"])
    assert by_dataset.depends_on_dataset_row_id == cloned.result.new_id(DatasetRow, g.ids["row0"])
    assert by_dataset.depends_on_field_name == "col"  # plain column copied verbatim


def test_token_rewrite_in_note(cloned: SimpleNamespace) -> None:
    g = cloned.graph
    note = _bug(cloned.new).subtasks[0].notes[0]
    new_chore = cloned.result.new_id(Task, g.ids["chore"])
    new_inline = cloned.result.new_id(Dataset, g.ids["ds_inline"])
    assert f"t_{new_chore.hex}" in note.body
    assert f"t_{g.ids['chore'].hex}" not in note.body
    assert f"d_{new_inline.hex}" in note.detail
    assert f"d_{g.ids['ds_inline'].hex}" not in note.detail


def test_timestamps_reset_and_original_intact(cloned: SimpleNamespace) -> None:
    g = cloned.graph
    assert cloned.new.created_at is not None
    assert cloned.new.updated_at is not None
    original = cloned.session.get(Project, g.ids["project"])
    assert original.name == "Orig"  # source untouched
    assert original.id == g.ids["project"]


def test_s3_hook_runs_after_commit_for_managed_only(cloned: SimpleNamespace) -> None:
    new_managed = cloned.result.new_id(Dataset, cloned.graph.ids["ds_managed"])
    assert cloned.s3 == [new_managed]


def test_hook_phases_fire(cloned: SimpleNamespace) -> None:
    calls = cloned.calls
    assert calls["after_copy"] == [cloned.new]  # only Project configures after_copy
    assert len(calls["after_remap"]) == 1  # the single note
    assert len(calls["after_commit"]) == 2  # both datasets


def test_result_mapping_and_objects(cloned: SimpleNamespace) -> None:
    result = cloned.result
    # CloneResult is a {original -> copy} mapping; every copied original resolves.
    assert result[cloned.graph.project] is result.root
    # objects holds every new instance: project, milestone, 3 tasks, note, 2 datasets,
    # 2 inline rows, 2 links == 12
    assert len(result.objects) == 12
    assert len(result) == 12
