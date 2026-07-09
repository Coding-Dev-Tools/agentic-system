"""Workflow (DAG) definitions -- YAML or dicts, validated up front.

A workflow is a set of nodes; each node produces exactly one Task per run.
Edges are ``depends_on``. Validation rejects unknown deps and cycles at load
time, never at execution time.

Invariant: agents never decide DAG order — the engine does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


class WorkflowDefinitionError(ValueError):
    pass


@dataclass(frozen=True)
class NodeDef:
    id: str
    task_type: str
    depends_on: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)
    max_attempts: int = 3
    required_role: Optional[str] = None


@dataclass(frozen=True)
class WorkflowDef:
    name: str
    nodes: dict[str, NodeDef]
    description: str = ""

    def roots(self) -> list[NodeDef]:
        return [n for n in self.nodes.values() if not n.depends_on]

    def dependents_of(self, node_id: str) -> list[NodeDef]:
        return [n for n in self.nodes.values() if node_id in n.depends_on]

    def topo_order(self) -> list[str]:
        order, seen = [], set()

        def visit(nid: str, stack: tuple[str, ...]) -> None:
            if nid in stack:
                raise WorkflowDefinitionError(
                    f"workflow {self.name!r}: dependency cycle at {nid!r}")
            if nid in seen:
                return
            for dep in self.nodes[nid].depends_on:
                visit(dep, stack + (nid,))
            seen.add(nid)
            order.append(nid)

        for nid in self.nodes:
            visit(nid, ())
        return order


def from_dict(d: dict[str, Any]) -> WorkflowDef:
    name = (d.get("name") or "").strip()
    if not name:
        raise WorkflowDefinitionError("workflow needs a non-empty 'name'")
    raw_nodes = d.get("nodes") or []
    if not raw_nodes:
        raise WorkflowDefinitionError(f"workflow {name!r} has no nodes")
    nodes: dict[str, NodeDef] = {}
    for rn in raw_nodes:
        nid = (rn.get("id") or "").strip()
        ttype = (rn.get("task_type") or "").strip()
        if not nid or not ttype:
            raise WorkflowDefinitionError(
                f"workflow {name!r}: every node needs 'id' and 'task_type'")
        if nid in nodes:
            raise WorkflowDefinitionError(f"workflow {name!r}: duplicate node id {nid!r}")
        nodes[nid] = NodeDef(
            id=nid, task_type=ttype,
            depends_on=tuple(rn.get("depends_on") or ()),
            params=dict(rn.get("params") or {}),
            max_attempts=int(rn.get("max_attempts", 3)),
            required_role=rn.get("required_role"),
        )
    for n in nodes.values():
        for dep in n.depends_on:
            if dep not in nodes:
                raise WorkflowDefinitionError(
                    f"workflow {name!r}: node {n.id!r} depends on unknown node {dep!r}")
    wf = WorkflowDef(name=name, nodes=nodes, description=d.get("description", ""))
    wf.topo_order()  # raises on cycles
    return wf


def load_yaml(path: "str | Path") -> WorkflowDef:
    import yaml
    with open(path, encoding="utf-8") as f:
        return from_dict(yaml.safe_load(f))


def load_directory(dir_path: Optional["str | Path"] = None) -> dict[str, WorkflowDef]:
    """Load all *.yaml workflow definitions (default: this package's
    ``definitions/`` directory)."""
    base = Path(dir_path) if dir_path else Path(__file__).parent / "definitions"
    defs: dict[str, WorkflowDef] = {}
    for p in sorted(base.glob("*.yaml")):
        wf = load_yaml(p)
        defs[wf.name] = wf
    return defs


__all__ = ["NodeDef", "WorkflowDef", "WorkflowDefinitionError",
           "from_dict", "load_yaml", "load_directory"]
