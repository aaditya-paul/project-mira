import sys
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# Force UTF-8 output so box-drawing characters in the banner render correctly
# regardless of the Windows terminal's default codepage (e.g., cp1252)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

console = Console()

def print_banner():
    banner = Text("""
███╗   ███╗██╗██████╗  █████╗ 
████╗ ████║██║██╔══██╗██╔══██╗
██╔████╔██║██║██████╔╝███████║
██║╚██╔╝██║██║██╔══██╗██╔══██║
██║ ╚═╝ ██║██║██║  ██║██║  ██║
╚═╝     ╚═╝╚═╝╚═╝  ╚═╝╚═╝  ╚═╝

Minimal Local Agent Loop
""", style="bold magenta")

    # console.print(Panel(
    #     banner,
    #     border_style="bright_magenta",
    #     padding=(1, 3)
    # ))

    console.print(banner)
    
def print_user_prompt(prompt: str):
    console.print(f"\n[bold cyan]User Task:[/bold cyan] {prompt}\n")

def print_tool_call(provider: str, tool_name: str, args: dict):
    args_str = ", ".join([f"{k}={v}" for k, v in args.items()])
    if tool_name == "vision":
         args_str = "<taking screenshot>"
    
    text = Text()
    text.append(f"[{provider}] ", style="bold green")
    text.append(f"{tool_name}({args_str})", style="yellow")
    console.print(text)

def print_tool_result(result: str):
    # Truncate if it's too long (e.g. vision output)
    if len(result) > 100:
        result = result[:100] + "... [truncated]"
    console.print(f"  [dim]Result: {result}[/dim]")

def print_thought(thought: str):
    if thought.strip():
        console.print(Panel(thought.strip(), title="[bold blue]Mira's Thoughts[/bold blue]", border_style="blue", padding=(0, 1)))

def print_final_answer(answer: str):
    console.print(Panel(answer, title="[bold green]Task Complete[/bold green]"))

def print_error(error: str):
    console.print(f"[bold red]Error:[/bold red] {error}")
