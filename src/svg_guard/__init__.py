"""SVG Guard — detect and fix text overflow in SVG diagrams."""

__version__ = "0.1.0"

from .checker import check_svg, check_directory
from .fixer import fix_svg
