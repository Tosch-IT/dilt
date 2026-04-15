"""
Data model for Docker Images Tree
"""
from dataclasses import dataclass, field

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
    rootfs_layers: list[str] = field(default_factory=list)

@dataclass
class TreeLayerNode:
    """Node in our logical layer tree."""
    command: str
    children: list["TreeLayerNode"] = field(default_factory=list)
    image_layers: list[tuple[ImageMeta, LayerInfo]] = field(default_factory=list)
