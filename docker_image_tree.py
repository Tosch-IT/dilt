#!/home/tosch/scripts/dockerImagesTree/venv/bin/python3
"""
Docker Images Tree TUI
Visualizes the build history of Docker images as a shared tree,
grouping common ancestor layers.  Docker history is fetched in a
background thread so the UI stays responsive.
"""

from __future__ import annotations

import json
import subprocess
import pyperclip
import shlex
import os
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Callable

from rich.text import Text

import re

NORMALIZE_PATTERNS = [
    (re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}:\d+\b'), '<IP:PORT>'),
    (re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b'), '<IP>'),
    (re.compile(r'\b[a-fA-F0-9]{32,128}\b'), '<HASH>'),
    (re.compile(r'\b(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?|\d{4}[-/]\d{2}[-/]\d{2})|(?:\d{10,16})\b'), '<TIMESTAMP>'),
    (re.compile(r'\b(?:v\d+(?:\.\d+)*(?:-[a-zA-Z0-9]+)*|\d+\.\d+(?:\.\d+)*(?:-[a-zA-Z0-9]+)*)\b'), '<VERSION>'),
]

def normalize_command(command: str) -> str:
    res = command
    for p, repl in NORMALIZE_PATTERNS:
        res = p.sub(repl, res)
    return res

CONFIG_DIR = Path.home() / ".config" / "docker_image_tree" / "substitutions"
DEFAULT_SUBS_TEXT = '# Define custom regex replacements here.\n# Format: "<regex>" "<replacement>"\n# Example:\n# "^#(nop).*$" "<NOP>"\n\n'

def parse_user_substitutions(raw_text: str) -> tuple[str, list[tuple[re.Pattern, str]], bool]:
    lines = raw_text.splitlines(keepends=True)
    patterns = []
    invalid = False
    new_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            new_lines.append(line)
            continue

        try:
            tokens = shlex.split(stripped)
            if len(tokens) != 2:
                raise ValueError("Line must contain exactly two quoted strings.")
            pat = re.compile(tokens[0])
            repl = tokens[1]
            patterns.append((pat, repl))
            new_lines.append(line)
        except Exception as e:
            invalid = True
            new_lines.append(f"# INVALID ({e}): {line.lstrip()}")

    return "".join(new_lines), patterns, invalid

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.command import Provider, Hit, DiscoveryHit
from textual.widgets import (
    Footer,
    Header,
    Input,
    OptionList,
    Label,
    LoadingIndicator,
    Static,
    TabbedContent,
    TabPane,
    DataTable,
    Tabs,
    Tree,
    Button,
)
from textual.widgets.tree import TreeNode
from textual.containers import Center, Middle, Vertical, Horizontal


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class LayerInfo:
    """One layer as reported by `docker history`."""
    created_by: str
    created_at: str
    layer_id: str          # sha256:... or <missing>
    size: str


@dataclass
class ImageMeta:
    """Top-level image metadata."""
    image_id: str          # short id
    repo_tag: str          # repo:tag  or  <untagged>
    digest: str            # sha256 of the image, or <none>
    layers: list[LayerInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Docker data collection  (runs in background thread)
# ---------------------------------------------------------------------------

def run_json_lines(cmd: list[str]) -> list[dict]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    rows = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def collect_images(
    on_progress: Optional["Callable[[int, int, str], None]"] = None,
    show_all: bool = False
) -> list[ImageMeta]:
    """
    Collect all images and their layer histories.
    on_progress(current, total, repo_tag) called after each history fetch.
    """
    if on_progress:
        on_progress(0, 0, "Listing all images…")  # phase: before loop

    cmd = [
        "docker", "images", "--no-trunc", "--digests",
        "--format", "{{json .}}"
    ]
    if show_all:
        cmd.insert(2, "--all")
    raw_images = run_json_lines(cmd)
    total = len(raw_images)

    images: list[ImageMeta] = []
    for idx, raw in enumerate(raw_images, start=1):
        repo = raw.get("Repository", "")
        tag  = raw.get("Tag", "")
        repo_tag = f"{repo}:{tag}" if repo and repo != "<none>" else "<untagged>"
        digest = raw.get("Digest", "<none>") or "<none>"
        image_id = raw.get("ID", "")

        history_rows = run_json_lines([
            "docker", "history", "--no-trunc",
            "--format", "{{json .}}",
            image_id
        ])

        layers: list[LayerInfo] = []
        for h in history_rows:
            layers.append(LayerInfo(
                created_by=h.get("CreatedBy", ""),
                created_at=h.get("CreatedAt", ""),
                layer_id=h.get("ID", "<missing>") or "<missing>",
                size=h.get("Size", ""),
            ))

        images.append(ImageMeta(
            image_id=image_id,
            repo_tag=repo_tag,
            digest=digest,
            layers=layers,
        ))

        # Report *after* history fetch so counter reflects completed work
        if on_progress:
            on_progress(idx, total, repo_tag)

    return images


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------

@dataclass
class TreeLayerNode:
    """Node in our logical layer tree."""
    command: str
    children: list["TreeLayerNode"] = field(default_factory=list)
    image_layers: list[tuple[ImageMeta, LayerInfo]] = field(default_factory=list)


def _layers_reversed(image: ImageMeta) -> list[LayerInfo]:
    """docker history lists newest first; reverse to get oldest-first (root→tip)."""
    return list(reversed(image.layers))


def build_tree(images: list[ImageMeta], combine_versions: bool = False, custom_patterns: list[tuple[re.Pattern, str]] = None) -> list[TreeLayerNode]:
    """
    Build a shared-history tree.  Layers with the same command at the same
    depth under the same parent are merged into one node.
    """
    roots: list[TreeLayerNode] = []

    def find_or_create(nodes: list[TreeLayerNode], command: str) -> TreeLayerNode:
        for n in nodes:
            if n.command == command:
                return n
        new_node = TreeLayerNode(command=command)
        nodes.append(new_node)
        return new_node

    for image in images:
        layers = _layers_reversed(image)
        current_level = roots
        for layer in layers:
            cmd = layer.created_by
            if combine_versions:
                cmd = normalize_command(cmd)
            if custom_patterns:
                for p, repl in custom_patterns:
                    cmd = p.sub(repl, cmd)
            node = find_or_create(current_level, cmd)
            node.image_layers.append((image, layer))
            current_level = node.children

    return roots


# ---------------------------------------------------------------------------
# Loading modal screen
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Command Palette & Config Modals
# ---------------------------------------------------------------------------

class SubstitutionsCommandProvider(Provider):
    async def discover(self):
        if hasattr(self.screen.app, "_current_config_name") and self.screen.app._current_config_name:
            yield DiscoveryHit("Save Substitutions Config", lambda app=self.screen.app: app.action_save_subs(), help=f"Save substitutions to {self.screen.app._current_config_name}")
        yield DiscoveryHit("Save Substitutions Config As...", lambda app=self.screen.app: app.action_save_subs_as(), help="Save current substitutions to disk with a new name")
        yield DiscoveryHit("Load Substitutions Config", lambda app=self.screen.app: app.action_load_subs(), help="Load saved substitutions from disk")

    async def search(self, query: str):
        matcher = self.matcher(query)
        if hasattr(self.screen.app, "_current_config_name") and self.screen.app._current_config_name:
            match0 = matcher.match("Save Substitutions Config")
            if match0 > 0:
                yield Hit(match0, matcher.highlight("Save Substitutions Config"), lambda app=self.screen.app: app.action_save_subs(), help=f"Save substitutions to {self.screen.app._current_config_name}")

        match1 = matcher.match("Save Substitutions Config As...")
        if match1 > 0:
            yield Hit(match1, matcher.highlight("Save Substitutions Config As..."), lambda app=self.screen.app: app.action_save_subs_as(), help="Save current substitutions to disk with a new name")

        match2 = matcher.match("Load Substitutions Config")
        if match2 > 0:
            yield Hit(match2, matcher.highlight("Load Substitutions Config"), lambda app=self.screen.app: app.action_load_subs(), help="Load saved substitutions from disk")

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


# ---------------------------------------------------------------------------
# Filter modal screen
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class DockerTreeApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #tree-panel {
        height: 50%;
        border: solid $accent;
        padding: 0 1;
    }

    #detail-panel {
        height: 50%;
        border: solid $accent;
    }

    Tree {
        scrollbar-gutter: stable;
    }

    TabbedContent {
        height: 100%;
    }

    ContentSwitcher {
        height: 1fr;
    }

    TabPane {
        padding: 0 1;
        height: 100%;
    }

    #cmd-label {
        color: $text;
        padding: 1;
    }

    DataTable {
        height: 1fr;
    }

    Header {
        dock: top;
    }

    Footer {
        dock: bottom;
    }
    """

    COMMANDS = App.COMMANDS | {SubstitutionsCommandProvider}

    BINDINGS = [
        Binding("j", "cursor_down", "Down",     show=False),
        Binding("k", "cursor_up",   "Up",       show=False),
        Binding("l", "expand_node", "Expand",   show=False),
        Binding("h", "collapse_node","Collapse", show=False),
        Binding("L", "expand_to_branch", "Expand to Branch", show=False),
        Binding("H", "collapse_branch", "Collapse Branch", show=False),
        Binding("u", "prev_tab",    "Prev tab", show=False),
        Binding("i", "next_tab",    "Next tab", show=False),
        Binding("y", "copy_cell",   "Copy Cell"),
        Binding("c", "toggle_compact", "Toggle Compact IDs", show=False),
        Binding("v", "toggle_combine", "Combine Versions"),
        Binding("e", "edit_substitutions", "Edit Substitutions"),
        Binding("a", "toggle_all",  "Toggle All (Dangling)"),
        Binding("f", "filter",      "Filter Branches"),
        Binding("q", "quit",        "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self._images: list[ImageMeta] = []
        self._tree_roots: list[TreeLayerNode] = []
        self._node_map: dict[int, TreeLayerNode] = {}
        self._selected_layer_node: Optional[TreeLayerNode] = None
        self._show_all: bool = False
        self._filter_string: str = ""
        self._compact_mode: bool = True
        self._combine_versions: bool = False
        self._custom_patterns_raw: str = DEFAULT_SUBS_TEXT
        self._custom_patterns: list[tuple[re.Pattern, str]] = []
        self._current_config_name: Optional[str] = None

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="tree-panel"):
            yield Tree("Docker Image Layers", id="layer-tree")
        with Vertical(id="detail-panel"):
            with TabbedContent(id="tabs", initial="tab-images"):
                with TabPane("Images", id="tab-images"):
                    yield DataTable(id="images-table")
                with TabPane("Full Command", id="tab-cmd"):
                    yield Static("Select a layer in the tree above.", id="cmd-label")
        yield Footer()

    # ------------------------------------------------------------------
    # Mount → kick off background worker
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self.title = "Docker Image Layer Tree"
        self.sub_title = "Loading…"
        self._setup_table()
        # Show loading modal, then start background fetch
        self.push_screen(LoadingScreen())
        self._fetch_docker_data()

    def _setup_table(self) -> None:
        table: DataTable = self.query_one("#images-table", DataTable)
        table.add_columns(
            "Layer Digest",
            "Built At",
            "Final Image Tag",
            "Final Image ID",
            "Final Image Digest",
        )

    # ------------------------------------------------------------------
    # Background worker – runs collect_images + build_tree in a thread
    # ------------------------------------------------------------------

    def _loading_phase(self, title: str, detail: str = "") -> None:
        """Update loading screen phase label (safe to call from main thread)."""
        if self.screen_stack and isinstance(self.screen_stack[-1], LoadingScreen):
            self.screen_stack[-1].update_phase(title, detail)

    @work(thread=True, exclusive=True)
    def _fetch_docker_data(self) -> None:
        def on_progress(current: int, total: int, repo_tag: str) -> None:
            screens = self.app.screen_stack
            if not (screens and isinstance(screens[-1], LoadingScreen)):
                return
            loading: LoadingScreen = screens[-1]
            if current == 0:  # initial phase: listing
                self.app.call_from_thread(
                    loading.update_phase,
                    "Fetching image list…",
                    "",
                )
            else:
                self.app.call_from_thread(
                    loading.update_progress, current, total, repo_tag
                )

        images = collect_images(on_progress=on_progress, show_all=self._show_all)

        # Phase: building the logical tree (CPU-only, fast but silent so far)
        self.app.call_from_thread(
            self._loading_phase,
            "Building layer tree…",
            f"{len(images)} image(s) — merging common ancestors",
        )
        tree_roots = build_tree(images, combine_versions=self._combine_versions, custom_patterns=self._custom_patterns)

        # Hand off to main thread; use call_later so the phase label renders
        self.app.call_from_thread(self._on_data_ready, images, tree_roots)

    def _on_data_ready(
        self,
        images: list[ImageMeta],
        tree_roots: list[TreeLayerNode],
    ) -> None:
        self._images = images
        self._tree_roots = tree_roots
        # Show "Populating UI" and defer the blocking work by one event-loop
        # tick so the label actually renders before we block the main thread.
        self._loading_phase(
            "Populating UI tree…",
            f"{len(images)} image(s) — building widgets",
        )
        self.call_later(self._populate_and_dismiss)

    # ------------------------------------------------------------------
    # Tree population
    # ------------------------------------------------------------------

    def _apply_filter_and_rebuild(self) -> None:
        if self._filter_string:
            fstr = self._filter_string.lower()
            filtered = []
            for img in self._images:
                match = fstr in img.repo_tag.lower() or fstr in img.image_id.lower() or fstr in img.digest.lower()
                if not match:
                    for l in img.layers:
                        if fstr in l.created_by.lower() or fstr in l.layer_id.lower():
                            match = True
                            break
                if match:
                    filtered.append(img)
        else:
            filtered = self._images

        self._tree_roots = build_tree(filtered, combine_versions=self._combine_versions, custom_patterns=self._custom_patterns)
        with self.batch_update():
            self._node_map.clear()
            self._populate_tree()

        tree = self.query_one("#layer-tree", Tree)
        if tree.root.children:
            tree.cursor_line = 0

        count = len(filtered)
        self.sub_title = f"{count} image(s) shown" + (f" (filtered: '{self._filter_string}')" if self._filter_string else "")

    def _populate_and_dismiss(self) -> None:
        """Called one event-loop tick after the phase label renders."""
        self._apply_filter_and_rebuild()
        if self.screen_stack and isinstance(self.screen_stack[-1], LoadingScreen):
            self.pop_screen()

        # Only watch once
        if not hasattr(self, "_cursor_watcher"):
            tree = self.query_one("#layer-tree", Tree)
            self._cursor_watcher = self.watch(tree, "cursor_line", self._on_cursor_line_change, init=True)

    def _populate_tree(self) -> None:
        tree: Tree = self.query_one("#layer-tree", Tree)
        for child in list(tree.root.children):
            child.remove()
        tree.root.expand()
        # Iterative (no recursion limit risk, avoids deep Python call stack)
        width = self.size.width - 6
        stack: list[tuple[TreeNode, TreeLayerNode]] = [
            (tree.root, root_node) for root_node in reversed(self._tree_roots)
        ]
        while stack:
            parent, layer_node = stack.pop()
            label = self._make_label(layer_node.command or "<empty>", max(20, width))
            node = parent.add(label, data=id(layer_node))
            self._node_map[id(layer_node)] = layer_node
            # Push children in reverse so left-most child is processed first
            for child in reversed(layer_node.children):
                stack.append((node, child))

    def _truncate_cmd(self, cmd: str, max_len: int) -> str:
        cmd = cmd.replace("\n", " ").strip()
        if len(cmd) > max_len:
            return cmd[: max_len - 3] + "…"
        return cmd

    def _make_label(self, cmd: str, max_len: int) -> Text:
        """Return a Rich Text label that is never parsed as markup."""
        return Text(self._truncate_cmd(cmd, max_len))

    # ------------------------------------------------------------------
    # Cursor watch + detail panel
    # ------------------------------------------------------------------

    def _on_cursor_line_change(self, cursor_line: int) -> None:
        """Called whenever the tree cursor moves (j/k/click/arrows)."""
        tree = self.query_one("#layer-tree", Tree)
        node = tree.get_node_at_line(cursor_line)
        if node is None or node.data is None:
            return
        layer_node = self._node_map.get(node.data)
        if layer_node is None:
            return
        self._selected_layer_node = layer_node
        self._update_details(layer_node)

    def _update_details(self, layer_node: TreeLayerNode) -> None:
        # Use Text() everywhere – Docker strings contain [ ] which Rich
        # would mis-parse as markup tags, silently throwing MarkupError.
        cmd_label: Static = self.query_one("#cmd-label", Static)
        cmd_label.update(Text(layer_node.command.replace(";  ", "\n") or "<empty>"))

        table: DataTable = self.query_one("#images-table", DataTable)
        table.clear()
        for image, layer in layer_node.image_layers:
            image_id_str = image.image_id
            if self._compact_mode:
                image_id_str = image_id_str[7:19] if image_id_str.startswith("sha256:") else image_id_str[:12]

            table.add_row(
                Text(layer.layer_id),
                Text(layer.created_at),
                Text(image.repo_tag),
                Text(image_id_str),
                Text(image.digest),
            )

    # ------------------------------------------------------------------
    # Key actions + node selection
    # ------------------------------------------------------------------

    def _update_from_cursor(self) -> None:
        """Read current cursor_node and update detail panel immediately."""
        tree = self.query_one("#layer-tree", Tree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        layer_node = self._node_map.get(node.data)
        if layer_node is not None:
            self._selected_layer_node = layer_node
            self._update_details(layer_node)

    @on(Tree.NodeSelected)
    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Handles Enter key and mouse click on a node."""
        if event.node.data is not None:
            layer_node = self._node_map.get(event.node.data)
            if layer_node is not None:
                self._selected_layer_node = layer_node
                self._update_details(layer_node)

    def action_cursor_down(self) -> None:
        self.query_one("#layer-tree", Tree).action_cursor_down()
        self.call_later(self._update_from_cursor)

    def action_cursor_up(self) -> None:
        self.query_one("#layer-tree", Tree).action_cursor_up()
        self.call_later(self._update_from_cursor)

    def action_expand_node(self) -> None:
        tree: Tree = self.query_one("#layer-tree", Tree)
        if tree.cursor_node:
            tree.cursor_node.expand()
        self.call_later(self._update_from_cursor)

    def action_collapse_node(self) -> None:
        tree: Tree = self.query_one("#layer-tree", Tree)
        if tree.cursor_node:
            tree.cursor_node.collapse()
        self.call_later(self._update_from_cursor)

    def action_expand_to_branch(self) -> None:
        tree: Tree = self.query_one("#layer-tree", Tree)
        node = tree.cursor_node
        if node:
            while True:
                node.expand()
                if len(node.children) == 1:
                    node = node.children[0]
                else:
                    break
        tree.move_cursor(node)
        # There seems to be a bug in textual. If only called oncce, we sometimes end up at the root, instead
        tree.move_cursor(node)
        self.call_later(self._update_from_cursor)

    def action_collapse_branch(self) -> None:
        tree: Tree = self.query_one("#layer-tree", Tree)
        node = tree.cursor_node
        prev = node
        while node and node != tree.root:
            node.collapse()
            prev = node
            node = node.parent
        tree.move_cursor(prev)
        self.call_later(self._update_from_cursor)

    def action_prev_tab(self) -> None:
        self.query_one("#tabs Tabs", Tabs).action_previous_tab()

    def action_next_tab(self) -> None:
        self.query_one("#tabs Tabs", Tabs).action_next_tab()

    def action_filter(self) -> None:
        def check_filter(substring: str | None) -> None:
            if substring is not None:
                self._filter_string = substring
                self._apply_filter_and_rebuild()
        self.push_screen(FilterScreen(), check_filter)

    def action_toggle_all(self) -> None:
        self._show_all = not self._show_all
        self.push_screen(LoadingScreen())
        self._fetch_docker_data()

    def action_toggle_compact(self) -> None:
        self._compact_mode = not self._compact_mode
        if self._selected_layer_node:
            self._update_details(self._selected_layer_node)

    def action_toggle_combine(self) -> None:
        self._combine_versions = not self._combine_versions
        self._apply_filter_and_rebuild()

    def action_edit_substitutions(self) -> None:
        editor = os.environ.get("EDITOR", "nano")
        fd, temp_path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, 'w') as f:
            f.write(self._custom_patterns_raw)

        with self.app.suspend():
            subprocess.run([editor, temp_path])

        with open(temp_path, 'r') as f:
            new_text = f.read()

        os.remove(temp_path)

        new_text, patterns, invalid = parse_user_substitutions(new_text)
        self._custom_patterns_raw = new_text
        self._custom_patterns = patterns

        if invalid:
            self.notify("Some substitutions were invalid and commented out.", severity="warning")

        self._apply_filter_and_rebuild()

    def action_save_subs(self) -> None:
        if self._current_config_name:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            (CONFIG_DIR / self._current_config_name).write_text(self._custom_patterns_raw)
            self.notify(f"Saved to {self._current_config_name}", title="Config Saved")

    def action_save_subs_as(self) -> None:
        def check_save(filename: str | None) -> None:
            if filename:
                def do_save(overwrite: bool) -> None:
                    if overwrite:
                        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                        (CONFIG_DIR / filename).write_text(self._custom_patterns_raw)
                        self._current_config_name = filename
                        self.notify(f"Saved to {filename}", title="Config Saved")

                if (CONFIG_DIR / filename).exists():
                    self.push_screen(ConfirmOverwriteScreen(filename), do_save)
                else:
                    do_save(True)

        self.push_screen(SaveConfigScreen(), check_save)

    def action_load_subs(self) -> None:
        def check_load(filename: str | None) -> None:
            if filename and (CONFIG_DIR / filename).exists():
                text = (CONFIG_DIR / filename).read_text()
                new_text, patterns, invalid = parse_user_substitutions(text)
                self._custom_patterns_raw = new_text
                self._custom_patterns = patterns
                self._current_config_name = filename
                if invalid:
                    self.notify("Some loaded substitutions were invalid and commented out.", severity="warning")
                self._apply_filter_and_rebuild()
                self.notify(f"Loaded {filename}", title="Config Loaded")
        self.push_screen(LoadConfigScreen(), check_load)

    def action_copy_cell(self) -> None:
        focused = self.app.focused
        text_to_copy = ""

        if focused is not None and focused.id == "images-table" and isinstance(focused, DataTable):
            if focused.cursor_coordinate:
                try:
                    cell = focused.get_cell_at(focused.cursor_coordinate)
                    if hasattr(cell, "plain"):
                        text_to_copy = cell.plain
                    else:
                        text_to_copy = str(cell)
                except Exception:
                    pass
        else:
            tree = self.query_one("#layer-tree", Tree)
            if tree.cursor_node and getattr(tree.cursor_node, "data", None) is not None:
                layer_node = self._node_map.get(tree.cursor_node.data)
                if layer_node:
                    text_to_copy = layer_node.command

        if text_to_copy:
            try:
                pyperclip.copy(text_to_copy)
                self.notify(f"Copied: {text_to_copy[:40]}", title="Clipboard")
            except Exception as e:
                self.notify(f"Failed to copy: {e}", severity="error")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    DockerTreeApp().run()
