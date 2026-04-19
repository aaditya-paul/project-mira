import logging
import multiprocessing as mp
import queue
import time
from typing import Any, Optional

from agent.voice.engine import run_voice_engine

logger = logging.getLogger("mira.voice.coordinator")


class VoiceCoordinator:
    """Owns the isolated voice process and IPC channels."""

    def __init__(self, config: dict):
        self.config = config or {}
        self.voice_config = self.config.get("voice_engine", {})
        self.enabled = bool(self.voice_config.get("enabled", False))

        self._manager: Optional[mp.managers.SyncManager] = None
        self.shared_state = None
        self.stop_event: Optional[mp.synchronize.Event] = None

        self.task_events: Optional[mp.Queue] = None
        self.voice_commands: Optional[mp.Queue] = None
        self.voice_inputs: Optional[mp.Queue] = None

        self.process: Optional[mp.Process] = None

    def is_running(self) -> bool:
        return bool(self.process and self.process.is_alive())

    def _ensure_channels(self):
        if self.task_events is not None:
            return

        self.task_events = mp.Queue(maxsize=300)
        self.voice_commands = mp.Queue(maxsize=120)
        self.voice_inputs = mp.Queue(maxsize=80)
        self.stop_event = mp.Event()
        self._manager = mp.Manager()
        self.shared_state = self._manager.dict()

    def start(self) -> bool:
        if not self.enabled:
            logger.info("Voice engine disabled in config.")
            return False

        if self.is_running():
            return True

        self._ensure_channels()

        payload = {
            "voice_engine": self.config.get("voice_engine", {}),
            "user_profile": self.config.get("user_profile", {}),
            "personalities": self.config.get("personalities", {}),
        }

        self.process = mp.Process(
            target=run_voice_engine,
            args=(
                self.task_events,
                self.voice_commands,
                self.voice_inputs,
                self.shared_state,
                self.stop_event,
                payload,
            ),
            daemon=True,
            name="MiraVoiceEngine",
        )
        self.process.start()
        logger.info("Voice engine process started (pid=%s)", self.process.pid)
        return True

    def stop(self):
        if self.stop_event:
            self.stop_event.set()

        if self.process and self.process.is_alive():
            self.process.join(timeout=8.0)
            if self.process.is_alive():
                logger.warning("Voice engine did not stop in time; terminating process.")
                self.process.terminate()
                self.process.join(timeout=1.0)

        if self._manager:
            try:
                self._manager.shutdown()
            except Exception:
                pass

    def _safe_put(self, target_queue: mp.Queue, payload: dict) -> bool:
        try:
            target_queue.put_nowait(payload)
            return True
        except queue.Full:
            try:
                target_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                target_queue.put_nowait(payload)
                return True
            except queue.Full:
                return False

    def emit_event(self, event_type: str, **payload):
        if not self.enabled or not self.task_events:
            return

        message = {
            "type": event_type,
            "payload": payload,
            "timestamp": time.time(),
        }
        self._safe_put(self.task_events, message)

    def update_state(self, **state):
        if not self.enabled or self.shared_state is None:
            return

        try:
            for key, value in state.items():
                self.shared_state[key] = value
        except Exception as e:
            logger.debug("Failed to update voice shared state: %s", e)

    def poll_command(self) -> Optional[dict[str, Any]]:
        if not self.enabled or not self.voice_commands:
            return None

        try:
            return self.voice_commands.get_nowait()
        except queue.Empty:
            return None

    def poll_input_task(self) -> Optional[dict[str, Any]]:
        if not self.enabled or not self.voice_inputs:
            return None

        try:
            return self.voice_inputs.get_nowait()
        except queue.Empty:
            return None

    def submit_input_task(self, task: str, source: str = "voice"):
        if not self.enabled or not self.voice_inputs:
            return

        payload = {
            "task": (task or "").strip(),
            "source": source,
            "timestamp": time.time(),
        }
        if payload["task"]:
            self._safe_put(self.voice_inputs, payload)
