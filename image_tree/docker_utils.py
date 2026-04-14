"""
Docker data collection (runs in background thread) and tree builder calculation.
"""
import json
import subprocess
import re
from typing import Optional, Callable
from image_tree.models import ImageMeta, LayerInfo, TreeLayerNode
from image_tree.text_utils import normalize_command

def run_json_lines(cmd: list[str]) -> list[dict]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(f"Command '{cmd[0]}' not found. Is it installed and in your PATH?")

    if result.returncode != 0:
        if len(cmd) > 1 and cmd[1] == 'images':
            err = result.stderr.strip()
            raise RuntimeError(f"Failed to list images. Is Docker running? ({err})")
        return []

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
