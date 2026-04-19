import logging
import queue
import random
import re
import time
from difflib import SequenceMatcher
from typing import Optional

from agent.voice.personality import resolve_personality

logger = logging.getLogger("mira.voice.engine")


class _Speaker:
    def __init__(self, voice_cfg: dict, personality_name: str, personality_profile: dict):
        self.voice_cfg = voice_cfg
        self.personality_name = personality_name
        self.personality_profile = personality_profile
        self.available = False
        self.engine = None

        speech_cfg = voice_cfg.get("speech", {})
        self.base_rate = int(speech_cfg.get("rate", 180))
        self.base_volume = float(speech_cfg.get("volume", 1.0))

        try:
            import pyttsx3

            self.engine = pyttsx3.init()
            self._apply_voice_profile(personality_profile)
            self.available = True
        except Exception as e:
            logger.warning("TTS unavailable, using log fallback: %s", e)

    def _apply_voice_profile(self, personality_profile: dict):
        if not (self.engine and isinstance(personality_profile, dict)):
            return

        voice_profile = personality_profile.get("voice", {}) if isinstance(personality_profile, dict) else {}
        rate_multiplier = float(voice_profile.get("rate_multiplier", 1.0))
        volume = float(voice_profile.get("volume", self.base_volume))

        self.engine.setProperty("rate", max(120, int(self.base_rate * rate_multiplier)))
        self.engine.setProperty("volume", max(0.1, min(volume, 1.0)))

    def set_personality(self, personality_name: str, personality_profile: dict):
        self.personality_name = personality_name
        self.personality_profile = personality_profile or {}
        if self.available and self.engine:
            self._apply_voice_profile(self.personality_profile)

    def say(self, text: str):
        message = (text or "").strip()
        if not message:
            return

        if self.available and self.engine:
            try:
                self.engine.say(message)
                self.engine.runAndWait()
                return
            except Exception as e:
                logger.warning("TTS speak failed, using fallback: %s", e)

        logger.info("[voice] %s", message)


class _Listener:
    def __init__(self, voice_cfg: dict):
        self.voice_cfg = voice_cfg
        listening_cfg = voice_cfg.get("listening", {})
        self.enabled = bool(listening_cfg.get("enabled", False))
        self.mode = str(listening_cfg.get("mode", "wake_word")).lower()
        self.wake_word = str(listening_cfg.get("wake_word", "mira")).strip().lower()

        default_aliases = ["meera", "miraa", "mera", "mirror"]
        configured_aliases = [
            str(alias).strip().lower()
            for alias in listening_cfg.get("wake_word_aliases", [])
            if str(alias).strip()
        ]
        self.wake_aliases = []
        for alias in [self.wake_word, *default_aliases, *configured_aliases]:
            if alias and alias not in self.wake_aliases:
                self.wake_aliases.append(alias)

        try:
            threshold = float(listening_cfg.get("wake_word_match_threshold", 0.7))
        except Exception:
            threshold = 0.7
        self.wake_match_threshold = max(0.5, min(threshold, 0.95))

        self.timeout = float(listening_cfg.get("listen_timeout_seconds", 2.0))
        self.phrase_limit = float(listening_cfg.get("phrase_time_limit_seconds", 6.0))

        self.available = False
        self._recognizer = None
        self._microphone = None
        self._sr = None
        self._ambient_calibrated = False

        if not self.enabled:
            return

        try:
            import speech_recognition as sr

            self._sr = sr
            self._recognizer = sr.Recognizer()
            self._microphone = sr.Microphone()
            self.available = True
        except Exception as e:
            logger.warning("STT unavailable, listening disabled: %s", e)

    def listen_once(self) -> Optional[str]:
        if not (self.enabled and self.available and self._recognizer and self._microphone):
            return None

        try:
            with self._microphone as source:
                if not self._ambient_calibrated:
                    self._recognizer.adjust_for_ambient_noise(source, duration=0.35)
                    self._ambient_calibrated = True
                audio = self._recognizer.listen(
                    source,
                    timeout=self.timeout,
                    phrase_time_limit=self.phrase_limit,
                )

            transcript = self._recognizer.recognize_google(audio)
            if transcript:
                return transcript.strip()
            return None
        except self._sr.WaitTimeoutError:
            return None
        except self._sr.UnknownValueError:
            return None
        except Exception as e:
            logger.debug("Listening cycle skipped: %s", e)
            return None


class VoiceEngine:
    def __init__(self, task_events, voice_commands, voice_inputs, shared_state, stop_event, payload: dict):
        self.task_events = task_events
        self.voice_commands = voice_commands
        self.voice_inputs = voice_inputs
        self.shared_state = shared_state
        self.stop_event = stop_event

        self.payload = payload or {}
        self.config = {
            "voice_engine": self.payload.get("voice_engine", {}),
            "user_profile": self.payload.get("user_profile", {}),
            "personalities": self.payload.get("personalities", {}),
        }

        self.voice_cfg = self.config.get("voice_engine", {})
        self.user_name = self.config.get("user_profile", {}).get("name", "friend")

        self.personality_name, self.personality_profile, _ = resolve_personality(self.config)

        self.speaker = _Speaker(self.voice_cfg, self.personality_name, self.personality_profile)
        self.listener = _Listener(self.voice_cfg)

        proactive_cfg = self.voice_cfg.get("proactive", {})
        self.proactive_mode = str(proactive_cfg.get("mode", "event_driven")).lower()
        self.idle_checkin_seconds = int(proactive_cfg.get("idle_checkin_seconds", 120))
        self.min_seconds_between_messages = int(proactive_cfg.get("min_seconds_between_messages", 20))

        self._last_spoken_at = 0.0
        self._last_checkin_at = 0.0
        self._last_listen_at = 0.0
        self.listen_poll_seconds = float(self.voice_cfg.get("listening", {}).get("poll_interval_seconds", 0.25))

    def run(self):
        self._speak_once(self._style_line("startup", "Voice companion is online."))

        while not self.stop_event.is_set():
            self._drain_events()
            self._maybe_proactive_checkin()
            self._maybe_listen()
            time.sleep(0.08)

    def _safe_put(self, out_queue, payload: dict):
        try:
            out_queue.put_nowait(payload)
        except queue.Full:
            try:
                out_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                out_queue.put_nowait(payload)
            except queue.Full:
                logger.debug("Queue still full after drop: %s", payload.get("action") or payload.get("type"))

    def _speak_once(self, text: str):
        if not text:
            return

        now = time.time()
        if now - self._last_spoken_at < self.min_seconds_between_messages:
            return

        self.speaker.say(text)
        self._last_spoken_at = now

    def _style_line(self, category: str, message: str) -> str:
        category = (category or "").strip().lower()

        if self.personality_name == "hype":
            presets = {
                "startup": [
                    f"Yo {self.user_name}, we are live. Ready when you are.",
                    "Voice mode is up. Let us move fast.",
                ],
                "checkin": [
                    "I am here if you want to run something next.",
                    "Still with you. Say what you want and I will jump in.",
                ],
            }
        elif self.personality_name == "calm_mentor":
            presets = {
                "startup": [
                    f"Hello {self.user_name}. Voice companion is ready.",
                    "Everything is set. We can proceed whenever you want.",
                ],
                "checkin": [
                    "I am still here. Share the next task when ready.",
                    "No rush. I can start the next action when you ask.",
                ],
            }
        else:
            presets = {
                "startup": [
                    f"Hey {self.user_name}, I am here and listening.",
                    "Voice companion is ready. Tell me what you want to do.",
                ],
                "checkin": [
                    "I am around if you want to run another task.",
                    "Still here with you. Say something when you are ready.",
                ],
            }

        if category in presets:
            return random.choice(presets[category])
        return message

    def _event_to_line(self, event_type: str, payload: dict) -> Optional[str]:
        event_type = (event_type or "").strip().lower()

        if event_type == "task_start":
            task = payload.get("task", "")
            return f"Starting: {task}" if task else "Starting your task."

        if event_type == "task_completed":
            return payload.get("summary") or "Task complete."

        if event_type == "task_cancelled":
            return "Okay, I cancelled that task."

        if event_type == "task_failed":
            reason = payload.get("reason", "")
            return f"That task failed: {reason}" if reason else "That task failed."

        if event_type == "step_failed":
            step = payload.get("step", "")
            return f"Step {step} failed. I am trying recovery." if step else "A step failed."

        if event_type == "status_report":
            return payload.get("message", "")

        if event_type == "personality_changed":
            requested = payload.get("personality_key") or payload.get("personality", "")
            if requested:
                resolved, profile, _ = resolve_personality(self.config, requested)
                self.personality_name = resolved
                self.personality_profile = profile
                self.speaker.set_personality(resolved, profile)
            return f"Personality switched to {payload.get('personality', 'new mode')}."

        return None

    def _drain_events(self):
        handled = 0
        while handled < 20:
            try:
                event = self.task_events.get_nowait()
            except queue.Empty:
                break

            handled += 1
            event_type = event.get("type", "")
            payload = event.get("payload", {}) or {}
            line = self._event_to_line(event_type, payload)
            if line:
                self._speak_once(line)

    def _maybe_proactive_checkin(self):
        if self.proactive_mode != "always_chatty":
            return

        now = time.time()
        if now - self._last_checkin_at < self.idle_checkin_seconds:
            return

        phase = str(self.shared_state.get("phase", "idle")).lower() if self.shared_state else "idle"
        if phase == "running":
            return

        self._last_checkin_at = now
        self._speak_once(self._style_line("checkin", "I am here if you need me."))

    def _normalize_transcript(self, transcript: str) -> tuple[str, bool]:
        text = (transcript or "").strip()
        if not text:
            return "", False

        lowered = text.lower()
        if self.listener.mode == "always_on":
            return text, True

        if self.listener.mode == "wake_word":
            wake_end = self._find_wake_word_end(text)
            if wake_end != -1:
                text = text[wake_end:].strip(" ,:.!?")
                return text, True
            return "", False

        # push_to_talk is treated as wake_word fallback for now.
        wake_end = self._find_wake_word_end(text)
        if wake_end != -1:
            text = text[wake_end:].strip(" ,:.!?")
            return text, True
        return "", False

    def _is_wake_match(self, token: str) -> bool:
        probe = (token or "").strip().lower()
        if not probe:
            return False

        for wake in self.listener.wake_aliases:
            if probe == wake:
                return True

            # Prefix tolerance helps with partial recognitions like "miraa"/"meer".
            if (probe.startswith(wake) or wake.startswith(probe)) and min(len(probe), len(wake)) >= 3:
                return True

            similarity = SequenceMatcher(None, probe, wake).ratio()
            if similarity >= self.listener.wake_match_threshold:
                return True

        return False

    def _find_wake_word_end(self, transcript: str) -> int:
        lowered = (transcript or "").lower()
        for match in re.finditer(r"[a-z0-9']+", lowered):
            if self._is_wake_match(match.group(0)):
                return match.end()
        return -1

    def _parse_command(self, transcript: str) -> Optional[dict]:
        cleaned, accepted = self._normalize_transcript(transcript)
        if not accepted:
            return {
                "action": "heard_ignored",
                "reason": "wake_word_not_detected",
                "raw_text": transcript,
            }
        if not cleaned:
            return {
                "action": "heard_ignored",
                "reason": "wake_word_only",
                "raw_text": transcript,
            }

        lower = cleaned.lower().strip()

        if any(token in lower for token in ("pause", "hold on", "wait a sec")):
            return {"action": "pause_task", "raw_text": transcript}

        if any(token in lower for token in ("resume", "continue")):
            return {"action": "resume_task", "raw_text": transcript}

        if any(token in lower for token in ("cancel", "stop task", "abort")):
            return {"action": "cancel_task", "raw_text": transcript}

        if "status" in lower or "what are you doing" in lower:
            return {"action": "status", "raw_text": transcript}

        if lower.startswith("set personality "):
            personality = lower.replace("set personality ", "", 1).strip()
            if personality:
                return {
                    "action": "set_personality",
                    "personality": personality,
                    "raw_text": transcript,
                }

        if lower.startswith("switch personality to "):
            personality = lower.replace("switch personality to ", "", 1).strip()
            if personality:
                return {
                    "action": "set_personality",
                    "personality": personality,
                    "raw_text": transcript,
                }

        # Default interpretation: a task request.
        return {
            "action": "run_task_now",
            "task": cleaned,
            "raw_text": transcript,
        }

    def _maybe_listen(self):
        if not self.listener.enabled:
            return

        now = time.time()
        if now - self._last_listen_at < self.listen_poll_seconds:
            return

        self._last_listen_at = now
        transcript = self.listener.listen_once()
        if not transcript:
            return

        command = self._parse_command(transcript)
        if not command:
            return

        command["timestamp"] = time.time()

        if command.get("action") == "run_task_now":
            phase = str(self.shared_state.get("phase", "idle")).lower() if self.shared_state else "idle"
            if phase == "idle":
                self._safe_put(
                    self.voice_inputs,
                    {
                        "task": command.get("task", ""),
                        "raw_text": transcript,
                        "source": "voice",
                        "timestamp": time.time(),
                    },
                )
                self._speak_once("On it. Starting now.")
                return

        self._safe_put(self.voice_commands, command)


def run_voice_engine(task_events, voice_commands, voice_inputs, shared_state, stop_event, payload: dict):
    logging.basicConfig(
        filename="mira_debug.log",
        filemode="a",
        format="=> %(asctime)s | %(levelname)s | [%(name)s] %(message)s",
        level=logging.DEBUG,
    )

    try:
        engine = VoiceEngine(task_events, voice_commands, voice_inputs, shared_state, stop_event, payload)
        engine.run()
    except Exception as e:
        logger.exception("Voice engine crashed: %s", e)
