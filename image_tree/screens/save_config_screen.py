"""
Save Config Screen.
"""
from textual import on
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.containers import Vertical
from textual.widgets import Static, Input

class SaveConfigScreen(ModalScreen[str]):
    CSS = """
    SaveConfigScreen { align: center middle; background: $background 80%; }
    #save-dialog { width: 60%; height: auto; padding: 1 2; background: $surface; border: thick $accent; }
    """
    def compose(self) -> ComposeResult:
        with Vertical(id="save-dialog"):
            yield Static("Enter configuration name:")
            yield Input(placeholder="my_rules")
            yield Static("Press Enter to save, ESC to cancel.")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def on_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)
