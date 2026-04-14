"""
Textual Loading screens for Docker Images Tree.
"""
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.containers import Center, Middle, Vertical, Horizontal
from textual.widgets import Label, LoadingIndicator, Static, Button, Input, OptionList
from image_tree.config import CONFIG_DIR

class LoadingScreen(ModalScreen):
    """Fullscreen modal shown while docker data is being fetched."""

    # q must always be reachable, even while this modal is on top
    BINDINGS = [Binding("q", "quit", "Quit")]

    CSS = """
    LoadingScreen {
        align: center middle;
        background: $background 80%;
    }

    #loading-box {
        width: 60;
        height: 9;
        border: double $accent;
        background: $surface;
        padding: 1 2;
    }

    #loading-title {
        text-align: center;
        color: $accent;
        text-style: bold;
        margin-bottom: 0;
    }

    #loading-progress {
        text-align: center;
        color: $text-muted;
        height: 1;
        margin-bottom: 1;
    }

    #loading-current {
        text-align: center;
        color: $text;
        height: 1;
        overflow: hidden;
    }

    LoadingIndicator {
        height: 3;
    }
    """

    def compose(self) -> ComposeResult:
        with Middle():
            with Center():
                with Vertical(id="loading-box"):
                    yield Label("Fetching Docker history…", id="loading-title")
                    yield Label("", id="loading-progress")
                    yield Label("", id="loading-current")
                    yield LoadingIndicator()

    def action_quit(self) -> None:  # noqa: D401
        self.app.exit()

    def update_progress(self, current: int, total: int, repo_tag: str) -> None:
        self.query_one("#loading-progress", Label).update(
            f"[{current}/{total}]"
        )
        max_w = 54
        display = repo_tag if len(repo_tag) <= max_w else "…" + repo_tag[-(max_w - 1):]
        try:
            self.query_one("#loading-current", Label).update(display)
        except Exception:
            pass

    def update_phase(self, title: str, detail: str = "") -> None:
        """Switch the title/detail line (e.g. to 'Building tree…')."""
        try:
            self.query_one("#loading-title", Label).update(title)
            self.query_one("#loading-progress", Label).update(detail)
            self.query_one("#loading-current", Label).update("")
        except Exception:
            pass  # screen not yet fully mounted – ignore
