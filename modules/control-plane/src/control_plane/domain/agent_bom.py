"""Agent Bill of Materials (A-BOM) - Forensic data for every spawned agent."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any
from ulid import ULID

from .enums import AgentStatus, RiskClassification


@dataclass
class AgentBillOfMaterials:
    """Forensic data object for every spawned agent."""
    id: str = field(default_factory=lambda: f"agent_{ULID()}")
    mission_id: str = ""
    task_id: str | None = None
    name: str = ""
    role: str = ""
    model_provider: str = "google"
    model_name: str = "gemini-3.6-flash"
    model_version: str = "latest"
    assigned_tools: list[str] = field(default_factory=list)
    effective_authority: dict[str, Any] = field(default_factory=dict)
    risk_classification: RiskClassification = RiskClassification.MODERATE
    creation_rationale: str = ""
    system_prompt_hash: str = ""
    status: AgentStatus = AgentStatus.COMPILING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    activated_at: datetime | None = None
    terminated_at: datetime | None = None
    termination_reason: str | None = None
    tokens_consumed: int = 0
    api_calls_made: int = 0
    parent_agent_id: str | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "mission_id": self.mission_id, "task_id": self.task_id,
            "name": self.name, "role": self.role, "model_provider": self.model_provider,
            "model_name": self.model_name, "model_version": self.model_version,
            "assigned_tools": self.assigned_tools,
            "effective_authority": self.effective_authority,
            "risk_classification": self.risk_classification.value,
            "creation_rationale": self.creation_rationale,
            "system_prompt_hash": self.system_prompt_hash, "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "activated_at": self.activated_at.isoformat() if self.activated_at else None,
            "terminated_at": self.terminated_at.isoformat() if self.terminated_at else None,
            "termination_reason": self.termination_reason,
            "tokens_consumed": self.tokens_consumed, "api_calls_made": self.api_calls_made,
            "parent_agent_id": self.parent_agent_id,
        }
