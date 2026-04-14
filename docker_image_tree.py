#!/usr/bin/env python3
"""
Docker Images Tree TUI
Visualizes the build history of Docker images as a shared tree,
grouping common ancestor layers.  Docker history is fetched in a
background thread so the UI stays responsive.
"""

from __future__ import annotations
import subprocess

import pyperclip
import os
import tempfile
import re
from typing import Optional

from rich.text import Text

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import (
    Footer,
    Header,
    Static,
    TabbedContent,
    TabPane,
    DataTable,
    Tabs,
    Tree,
)
from textual.widgets.tree import TreeNode
from textual.containers import Vertical

# Import from our modules
from image_tree.models import ImageMeta, TreeLayerNode
from image_tree.config import CONFIG_DIR, DEFAULT_SUBS_TEXT
from image_tree.text_utils import parse_user_substitutions
from image_tree.docker_utils import collect_images, build_tree
from image_tree.screens import LoadingScreen, ConfirmOverwriteScreen, SaveConfigScreen, LoadConfigScreen, FilterScreen
from image_tree.commands import SubstitutionsCommandProvider

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
        Binding("1", "focus_tree",  "Tree", show=False),
        Binding("2", "focus_table", "Table", show=False),
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
    # Background worker
    # ------------------------------------------------------------------

    def _loading_phase(self, title: str, detail: str = "") -> None:
        """Update loading screen phase label (safe to call from main thread)."""
        if self.screen_stack and isinstance(self.screen_stack[-1], LoadingScreen):
            self.screen_stack[-1].update_phase(title, detail)

    def _on_fetch_error(self, error_msg: str) -> None:
        self.notify(f"Error: {error_msg}", title="Data Fetch Failed", severity="error", timeout=10.0)
        self._images = []
        self._tree_roots = []
        self.call_later(self._populate_and_dismiss)

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

        try:
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
        except Exception as e:
            self.app.call_from_thread(self._on_fetch_error, str(e))

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
        focused = self.app.focused
        if focused is not None and focused.id == "images-table":
            focused.action_cursor_down()
        else:
            self.query_one("#layer-tree", Tree).action_cursor_down()
            self.call_later(self._update_from_cursor)

    def action_cursor_up(self) -> None:
        focused = self.app.focused
        if focused is not None and focused.id == "images-table":
            focused.action_cursor_up()
        else:
            self.query_one("#layer-tree", Tree).action_cursor_up()
            self.call_later(self._update_from_cursor)

    def action_expand_node(self) -> None:
        focused = self.app.focused
        if focused is not None and focused.id == "images-table":
            focused.action_cursor_right()
        else:
            tree: Tree = self.query_one("#layer-tree", Tree)
            if tree.cursor_node:
                tree.cursor_node.expand()
            self.call_later(self._update_from_cursor)

    def action_collapse_node(self) -> None:
        focused = self.app.focused
        if focused is not None and focused.id == "images-table":
            focused.action_cursor_left()
        else:
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

    def action_focus_tree(self) -> None:
        self.query_one("#layer-tree", Tree).focus()

    def action_focus_table(self) -> None:
        table = self.query_one("#images-table", DataTable)
        target_tab = self.query_one("#tab-images", TabPane)
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = "tab-images"
        table.focus()
        if table.row_count > 0:
            try:
                table.move_cursor(row=0)
            except Exception:
                pass # fallback if move_cursor requires other args

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

if __name__ == "__main__":
    DockerTreeApp().run()
