"""
Configuration constants.
"""
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "docker_image_tree" / "substitutions"
DEFAULT_SUBS_TEXT = '# Define custom regex replacements here.\n# Format: "<regex>" "<replacement>"\n# Example:\n# "^#(nop).*$" "<NOP>"\n\n'
