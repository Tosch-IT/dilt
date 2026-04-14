"""
Load Config Screen.
"""
from textual import on
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.containers import Vertical
from textual.widgets import Static, OptionList
from image_tree.config import CONFIG_DIR

class LoadConfigScreen(ModalScreen[str]):
    CSS = """
    LoadConfigScreen { align: center middle; background: $background 80%; }
    #load-dialog { width: 60%; height: auto; padding: 1 2; background: $surface; border: thick $accent; }
    """
    def compose(self) -> ComposeResult:
        with Vertical(id="load-dialog"):
            yield Static("Select configuration to load (ESC to cancel):")
            yield OptionList()

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        if CONFIG_DIR.exists():
            for f in sorted(CONFIG_DIR.iterdir()):
                if f.is_file():
                    options.add_option(f.name)

    @on(OptionList.OptionSelected)
    def on_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.prompt)
