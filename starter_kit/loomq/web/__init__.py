"""The browser front end: a zero-install, zero-account way in.

``python -m loomq web`` serves a single self-contained page from the standard
library — no framework, no bundler, no CDN, and it works with the network cable
unplugged.  That is a design requirement, not a shortcut: the point of the
project is that using a quantum computer should not start with an install guide.
"""

from .server import serve

__all__ = ["serve"]
