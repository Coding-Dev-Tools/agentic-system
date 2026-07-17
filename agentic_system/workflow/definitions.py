"""Task and Workflow definitions for the DAG engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class TaskDef:
    """Static definition of a workflow task."""
    name: str
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    # Optional: custom executor key (host registers implementations)
    executor: Optional[str] = None
    # Idempotency key template (can reference inputs, e.g. "lint-{files_hash}")
    idempotency_key: Optional[str] = None


@dataclass(frozen=True)
class WorkflowDef:
    """Static definition of a workflow (DAG of tasks)."""
    name: str
    tasks: tuple[TaskDef, ...]

    def get_task(self, name: str) -> Optional[TaskDef]:
        for t in self.tasks:
            if t.name == name:
                return t
        return None

    def dependencies_of(self, task_name: str) -> list[str]:
        """Return task names that this task depends on (by inputs)."""
        task = self.get_task(task_name)
        if not task:
            return []
        deps = []
        for t in self.tasks:
            if any(out in task.inputs for out in t.outputs):
                deps.append(t.name)
        return deps

    def topological_order(self) -> list[TaskDef]:
        """Return tasks in topological order (Kahn's algorithm)."""
        # Build adjacency
        indeg = {t.name: 0 for t in self.tasks}
        adj = {t.name: [] for t in self.tasks}
        for t in self.tasks:
            for inp in t.inputs:
                for other in self.tasks:
                    if inp in other.outputs:
                        adj[other.name].append(t.name)
                        indeg[t.name] += 1
        # Kahn
        queue = [n for n, d in indeg.items() if d == 0]
        order = []
        while queue:
            n = queue.pop(0)
            order.append(self.get_task(n))
            for m in adj[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    queue.append(m)
        if len(order) != len(self.tasks):
            raise ValueError("workflow has cycles")
        return order