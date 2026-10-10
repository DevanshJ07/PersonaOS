from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class BehaviourProtocol(Protocol):
    """Common behaviour API used by office and browser modules."""

    def pause(self, action: str = "default") -> None:
        ...

    def type_text(self, text: str) -> None:
        ...


@dataclass
class SessionContext:
    config: dict[str, Any]
    output_dir: Path
    behaviour: BehaviourProtocol


@dataclass
class TaskResult:
    success: bool
    actions_completed: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
