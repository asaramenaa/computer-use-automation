"""The replay result contract returned to the calling agent."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RunStatus(StrEnum):
    SUCCESS = "SUCCESS"                    # checkpoints held, outputs returned
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"  # a declared, legitimate result (e.g. MEMBER_NOT_FOUND)
    FAILED = "FAILED"                      # hard failure with debuggable detail
    ESCALATED = "ESCALATED"                # handed to a human; run paused or abandoned


class EvidenceRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    screenshot: str | None = None
    a11y_snapshot: str | None = None
    log: str | None = None


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    description: str
    step_id: str


class Failure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str | None
    step_label: str | None
    expected: str
    observed: str
    evidence: EvidenceRefs = Field(default_factory=EvidenceRefs)


class Escalation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intervention_id: str
    step_id: str | None
    reason: str
    request_path: str


class RecoveryApplied(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recovery_id: str
    step_id: str
    attempt: int
    trigger: str


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: RunStatus
    run_id: str
    capability_id: str
    version: str
    started_at: datetime
    finished_at: datetime
    steps_completed: int = 0
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: Outcome | None = None
    failure: Failure | None = None
    escalation: Escalation | None = None
    recoveries_applied: list[RecoveryApplied] = Field(default_factory=list)
    evidence_dir: str

    @model_validator(mode="after")
    def _consistent(self) -> "RunResult":
        s = self.status
        if s is RunStatus.SUCCESS and (self.outcome or self.failure or self.escalation):
            raise ValueError("SUCCESS carries outputs only")
        if s is RunStatus.BUSINESS_OUTCOME and self.outcome is None:
            raise ValueError("BUSINESS_OUTCOME needs outcome")
        if s is RunStatus.FAILED and self.failure is None:
            raise ValueError("FAILED needs failure detail")
        if s is RunStatus.ESCALATED and self.escalation is None:
            raise ValueError("ESCALATED needs escalation detail")
        return self
