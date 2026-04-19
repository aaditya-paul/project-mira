import os
import sys
import time
import logging
import threading
import queue
from dotenv import load_dotenv
from agent.brain import AgentBrain
from agent.display import print_banner, print_user_prompt, console
from agent.voice.coordinator import VoiceCoordinator


class _ConsoleInputWorker:
    """Keeps console input off the main control loop so voice input can be polled concurrently."""

    def __init__(self, prompt: str):
        self.prompt = prompt
        self.queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                task = console.input(self.prompt)
            except KeyboardInterrupt:
                self.queue.put("__exit__")
                break
            except Exception:
                if not self._stop.is_set():
                    self.queue.put("")
                continue
            self.queue.put(task)

    def poll(self, timeout: float = 0.2) -> str | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self):
        self._stop.set()


def _print_listening_hint(config: dict):
    voice_cfg = (config or {}).get("voice_engine", {})
    listening_cfg = voice_cfg.get("listening", {})
    if not listening_cfg.get("enabled", False):
        return

    mode = str(listening_cfg.get("mode", "wake_word")).strip().lower()
    wake_word = str(listening_cfg.get("wake_word", "mira")).strip()
    wake_aliases = [
        str(alias).strip()
        for alias in listening_cfg.get("wake_word_aliases", [])
        if str(alias).strip()
    ]

    if mode == "wake_word":
        wake_terms = []
        for term in [wake_word, *wake_aliases]:
            lowered = term.lower()
            if lowered and lowered not in [t.lower() for t in wake_terms]:
                wake_terms.append(term)
        shown = ", ".join(f"'{term}'" for term in wake_terms[:5])
        console.print(f"[dim green]Listening for wake word ({shown})...[/dim green]")
    elif mode == "always_on":
        console.print("[dim green]Listening continuously...[/dim green]")
    else:
        console.print("[dim green]Listening enabled.[/dim green]")

def main():
    logging.basicConfig(
        filename='mira_debug.log',
        filemode='a',
        format='=> %(asctime)s | %(levelname)s | [%(name)s] %(message)s',
        level=logging.DEBUG
    )
    logging.info("=== Mira Application Started ===")
    
    load_dotenv()
    print_banner()
    
    voice_coordinator = None

    try:
        brain = AgentBrain()
    except Exception as e:
        console.print(f"[bold red]Failed to initialize Brain:[/bold red] {str(e)}")
        sys.exit(1)

    try:
        voice_coordinator = VoiceCoordinator(brain.config)
        if voice_coordinator.start():
            brain.attach_voice_coordinator(voice_coordinator)
            console.print("[dim green]Voice companion started in separate process.[/dim green]")
            _print_listening_hint(brain.config)
        else:
            console.print("[dim yellow]Voice companion disabled in config.[/dim yellow]")
    except Exception as e:
        logging.error(f"Voice coordinator failed to start: {e}", exc_info=True)
        console.print(f"[dim yellow]Voice companion unavailable: {str(e)}[/dim yellow]")

    console.print("[dim]Using Fallback Chain:[/dim] " + " -> ".join(brain.fallback_chain))

    input_worker = _ConsoleInputWorker("\n[bold cyan]What should Mira do?[/bold cyan] (or 'exit'): ")
    input_worker.start()
    
    try:
        while True:
            try:
                task = None
                task_source = "console"

                idle_voice_task = brain.process_idle_voice_commands()
                if idle_voice_task:
                    task = idle_voice_task
                    task_source = "voice"

                if not task and voice_coordinator:
                    voice_payload = voice_coordinator.poll_input_task()
                    if voice_payload:
                        raw_text = str(voice_payload.get("raw_text", "")).strip()
                        if raw_text:
                            console.print(f"[bold magenta]Voice Heard:[/bold magenta] {raw_text}")
                        task = voice_payload.get("task", "")
                        task_source = voice_payload.get("source", "voice")

                if not task:
                    task = input_worker.poll(timeout=0.25)
                    task_source = "console"

                if task is None:
                    continue

                task = task.strip()
                if not task:
                    continue

                if task.lower() in ["exit", "quit", "__exit__"]:
                    logging.info("User requested exit.")
                    break

                logging.info(f"USER TASK INPUT ({task_source}): {task}")
                print_user_prompt(task)
                result = brain.run_agentic_loop(task)

                if result and result.get("status") == "interrupted" and result.get("next_task"):
                    follow_up = str(result.get("next_task", "")).strip()
                    if follow_up and voice_coordinator:
                        voice_coordinator.submit_input_task(follow_up, source="voice-interrupt")

                logging.info(f"Finished loop for task: {task}")

            except KeyboardInterrupt:
                logging.info("Keyboard interrupt received. Exiting.")
                break
            except Exception as e:
                logging.error(f"Critical error in main loop: {str(e)}", exc_info=True)
                console.print(f"[bold red]Critical Error:[/bold red] {str(e)}")
                time.sleep(0.2)
    finally:
        input_worker.stop()
        if voice_coordinator:
            try:
                voice_coordinator.stop()
            except Exception:
                pass

if __name__ == "__main__":
    main()
