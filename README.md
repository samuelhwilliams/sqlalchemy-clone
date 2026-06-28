# sqlalchemy-clone

Deep-copy a SQLAlchemy ORM instance — and a chosen slice of its relationship graph — onto
**fresh primary keys**, with **automatic foreign-key remapping**. Native SQLAlchemy 2.0+,
cross-backend (validated on SQLite with foreign keys enforced; relies on nothing
Postgres-specific).

The whole surface is one function, `clone()`, plus a declarative plan, `CloneSpec`:

```python
from sqlalchemy_clone import clone, CloneSpec, Entity, Follow, Persist

spec = CloneSpec(entities={
    Project:   Entity(
        follow=[Project.milestones, Project.datasets],
        fields={Project.name: lambda old, new, ctx: f"{old.name} (copy)"},
    ),
    Milestone: Entity(follow=[Milestone.tasks]),
    Task:      Entity(                                   # one Entity serves all subclasses
        follow=[Task.subtasks, Task.notes, Task.refs],
        exclude=[Task.audit_logs],
    ),
    Dataset:   Entity(
        follow=[Follow(Dataset.rows, when=lambda d: d.kind == "inline")],
        after_commit=lambda old, new, ctx: push_to_s3(new) if new.kind == "managed" else None,
    ),
})

new_project = clone(project, spec, session=session, persist=Persist.COMMIT).root
```

## The one idea

You declare **which relationships to follow**; the library decides **how to wire foreign keys**
by a single rule:

> An FK is remapped to the new copy if its referent was itself copied; otherwise it is kept
> (it points outside the copied set). A NULL FK stays NULL.

So self-references (`Task.parent_id`), cross-link tables, parent links, and long-range
internal references all rewire to the new objects automatically, while external FKs
(`Project.owner_id`) are left untouched — with no per-column configuration.

## Capabilities

- Polymorphic single-table inheritance (the right subclass is constructed; discriminator preserved)
- Self-referential FKs and `ordering_list` (order columns copied verbatim, no renumbering)
- Cross-link tables with several FKs all pointing inside the copy set
- Cyclic and self-referential FKs handled by a library-owned two-phase write
  (no reliance on `DEFERRABLE` constraints)
- Client-side timestamps / server defaults reset so the database re-applies them
- `fields` overrides, per-FK `KEEP`/`REMAP`/`NULLOUT` actions, conditional `Follow(when=...)`
- Lifecycle hooks: `after_copy`, `after_remap` (complete `{original → copy}` map; for rewriting
  ids embedded in text/JSON), `after_flush`, and `after_commit` (rollback-safe external effects)

Columns and relationships may be named by string **or** by the mapped attribute
(`Project.name`, `Task.parent_id`) — attributes give autocomplete, refactor-safety, and
validation that the column belongs to the model.

## Status

Implemented and tested. Primary keys with a client-side default (e.g. `uuid4`) are minted
automatically; for keys without one (server-generated, sequence, or composite), supply
`CloneSpec.mint_pk` to provide the value (otherwise a `PrimaryKeyMintError` is raised).

## Develop

```sh
uv sync
uv run pytest          # add --pg-url postgresql+psycopg://… to also test against Postgres
uv run ty check
```
