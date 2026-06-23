"""SVG Guard — detect and fix text overflow in SVG diagrams."""

__version__ = "0.1.0"

from .checker import DetectionConfig as DetectionConfig
from .checker import check_directory as check_directory
from .checker import check_svg as check_svg
from .fixer import fix_svg as fix_svg
