"""
Filter Screen.
"""
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.containers import Center, Middle, Vertical
from textual.widgets import Label, Input

class FilterScreen(ModalScreen[str]):
    """Modal screen to ask for filter string."""

    BINDINGS = [Binding("escape", "quit", "Cancel")]

    CSS = """
    FilterScreen {
        align: center middle;
        background: $background 80%;
    }

    #filter-box {
        width: 60;
        height: auto;
        border: double $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Middle():
            with Center():
                with Vertical(id="filter-box"):
                    yield Label("Filter branches by substring:")
                    yield Input(placeholder="e.g. apt-get", id="filter-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def submit_filter(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_quit(self) -> None:
        self.dismiss("")
