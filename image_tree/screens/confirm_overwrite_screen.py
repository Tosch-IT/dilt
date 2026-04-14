"""
Confirm Overwrite Screen.
"""
from textual import on
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.containers import Vertical, Horizontal
from textual.widgets import Static, Button

class ConfirmOverwriteScreen(ModalScreen[bool]):
    CSS = """
    ConfirmOverwriteScreen { align: center middle; background: $background 80%; }
    #confirm-dialog { width: 40; height: auto; padding: 1 2; background: $surface; border: thick $accent; }
    #confirm-buttons { height: auto; align: center middle; margin-top: 1; }
    #confirm-buttons Button { margin: 0 1; }
    """
    def __init__(self, filename: str):
        super().__init__()
        self.filename = filename

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Static(f"File '{self.filename}' already exists.\nOverwrite?")
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", id="btn-yes", variant="warning")
                yield Button("No", id="btn-no", variant="primary")

    @on(Button.Pressed)
    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")
