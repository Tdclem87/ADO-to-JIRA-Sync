import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class AppConfig:
    ado_configs: dict[str, dict[str, str]]
    jira_base_url: str
    jira_email: str
    max_attachment_bytes: int
    config_path: Path
    log_dir: Path
    state_path: Path
    app_version: str


@dataclass
class RuntimeState:
    config: Optional[AppConfig] = None
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session: Any = None
    run_state: dict[str, Any] = field(default_factory=dict)
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("ado_to_jira_sync")
    )


RUNTIME = RuntimeState()