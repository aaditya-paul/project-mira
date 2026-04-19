"""
Agent State Memory — Tracks what the agent did, what it expects, and what happened.

This is the missing "brain" between execution steps. Instead of each step being
stateless, the agent now maintains a running memory of context, expectations,
and outcomes. This enables the verify→correct loop.
"""
import time
import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("mira.state")


class StepStatus(Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    VERIFIED = "verified"
    FAILED = "failed"
    RECOVERED = "recovered"
    SKIPPED = "skipped"


class RiskLevel(Enum):
    LOW = "low"          # typing text, scrolling — unlikely to break
    MEDIUM = "medium"    # hotkeys, app switches — might not work
    HIGH = "high"        # mouse clicks, coordinate-based — fragile


@dataclass
class StepRecord:
    """Record of a single executed step."""
    step_num: int
    action: str
    params: dict
    description: str
    expected_state: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    status: StepStatus = StepStatus.PENDING
    result: str = ""
    verify_reason: str = ""
    recovery_attempts: int = 0
    timestamp: float = field(default_factory=time.time)


class AgentState:
    """
    Tracks agent state between execution steps.
    
    This provides the state memory the agent was missing:
    - What app am I in?
    - What did I just do?
    - What should have happened?
    - What actually happened?
    """

    def __init__(self, task: str):
        self.task = task
        self.current_app: str = ""
        self.current_window_title: str = ""
        self.current_process: str = ""
        self.last_action: str = ""
        self.last_params: dict = {}
        self.expected_state: str = ""
        self.step_history: list[StepRecord] = []
        self.failed_steps: list[int] = []
        self._start_time = time.time()

    def begin_step(self, step: dict) -> StepRecord:
        """Mark a step as starting execution. Returns the StepRecord for tracking."""
        risk_str = step.get("risk_level", "low")
        try:
            risk = RiskLevel(risk_str)
        except ValueError:
            risk = RiskLevel.LOW

        record = StepRecord(
            step_num=step.get("step", len(self.step_history) + 1),
            action=step.get("action", ""),
            params=step.get("params", {}),
            description=step.get("description", ""),
            expected_state=step.get("expect", ""),
            risk_level=risk,
            status=StepStatus.EXECUTING,
        )
        self.step_history.append(record)

        # Update running state
        self.last_action = record.action
        self.last_params = record.params
        self.expected_state = record.expected_state

        # Track target app
        if record.action in ("switch_to_app", "launch_app"):
            self.current_app = record.params.get("app_name", self.current_app)

        logger.debug(
            f"Step {record.step_num} begun: {record.action} "
            f"(risk={record.risk_level.value}, expect={record.expected_state})"
        )
        return record

    def record_result(self, result: str):
        """Store the raw result of the most recent step."""
        if self.step_history:
            self.step_history[-1].result = result

    def update_window(self, title: str, process: str):
        """Update the tracked window state (called after cheap window check)."""
        self.current_window_title = title
        self.current_process = process

    def mark_verified(self, reason: str = ""):
        """Mark the most recent step as successfully verified."""
        if self.step_history:
            step = self.step_history[-1]
            step.status = StepStatus.VERIFIED
            step.verify_reason = reason
            logger.info(f"Step {step.step_num} VERIFIED: {reason}")

    def mark_failed(self, reason: str):
        """Mark the most recent step as failed verification."""
        if self.step_history:
            step = self.step_history[-1]
            step.status = StepStatus.FAILED
            step.verify_reason = reason
            self.failed_steps.append(step.step_num)
            logger.warning(f"Step {step.step_num} FAILED: {reason}")

    def mark_recovered(self, reason: str = ""):
        """Mark the most recent step as recovered after a failure."""
        if self.step_history:
            step = self.step_history[-1]
            step.status = StepStatus.RECOVERED
            step.verify_reason = reason
            # Remove from failed list since it recovered
            if step.step_num in self.failed_steps:
                self.failed_steps.remove(step.step_num)
            logger.info(f"Step {step.step_num} RECOVERED: {reason}")

    def increment_retry(self) -> int:
        """Increment the retry counter for the current step. Returns new count."""
        if self.step_history:
            self.step_history[-1].recovery_attempts += 1
            return self.step_history[-1].recovery_attempts
        return 0

    @property
    def current_step(self) -> StepRecord | None:
        """The most recent step record."""
        return self.step_history[-1] if self.step_history else None

    @property
    def total_failures(self) -> int:
        return len(self.failed_steps)

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self._start_time

    def get_recovery_context(self) -> str:
        """
        Build a compact context string for the LLM recovery prompt.
        Contains only what the LLM needs to diagnose and fix the issue.
        """
        step = self.current_step
        if not step:
            return "No step context available."

        # Last 3 steps for context
        recent = self.step_history[-3:]
        history_str = "\n".join(
            f"  Step {s.step_num}: {s.action}({s.params}) → {s.status.value}"
            + (f" ({s.verify_reason})" if s.verify_reason else "")
            for s in recent
        )

        return f"""=== RECOVERY CONTEXT ===
Task: {self.task}
Current App: {self.current_app}
Current Window: "{self.current_window_title}" ({self.current_process})
Failed Step: {step.step_num} — {step.description}
  Action: {step.action}({step.params})
  Expected: {step.expected_state}
  Got: {step.verify_reason}
  Retry Attempt: {step.recovery_attempts}

Recent History:
{history_str}
=== END RECOVERY CONTEXT ==="""

    def get_summary(self) -> str:
        """Human-readable summary of the entire execution."""
        verified = sum(1 for s in self.step_history if s.status == StepStatus.VERIFIED)
        recovered = sum(1 for s in self.step_history if s.status == StepStatus.RECOVERED)
        failed = sum(1 for s in self.step_history if s.status == StepStatus.FAILED)
        total = len(self.step_history)
        elapsed = self.elapsed_seconds

        return (
            f"Execution Summary: {verified}/{total} verified, "
            f"{recovered} recovered, {failed} failed "
            f"({elapsed:.1f}s elapsed)"
        )
