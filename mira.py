import os
import sys
import logging
from dotenv import load_dotenv
from agent.brain import AgentBrain
from agent.display import print_banner, print_user_prompt, console

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
    
    try:
        brain = AgentBrain()
    except Exception as e:
        console.print(f"[bold red]Failed to initialize Brain:[/bold red] {str(e)}")
        sys.exit(1)

    console.print("[dim]Using Fallback Chain:[/dim] " + " -> ".join(brain.fallback_chain))
    
    while True:
        try:
            task = console.input("\n[bold cyan]What should Mira do?[/bold cyan] (or 'exit'): ")
            if not task.strip():
                continue
            if task.lower() in ['exit', 'quit']:
                logging.info("User requested exit.")
                break
                
            logging.info(f"USER TASK INPUT: {task}")
            print_user_prompt(task)
            brain.run_agentic_loop(task)
            logging.info(f"Finished loop for task: {task}")
            
        except KeyboardInterrupt:
            logging.info("Keyboard interrupt received. Exiting.")
            break
        except Exception as e:
            logging.error(f"Critical error in main loop: {str(e)}", exc_info=True)
            console.print(f"[bold red]Critical Error:[/bold red] {str(e)}")

if __name__ == "__main__":
    main()
