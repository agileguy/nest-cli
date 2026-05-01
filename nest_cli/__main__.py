"""Entry point for ``python -m nest_cli`` and the ``nest-cli`` console script.

The real Click root group lives in ``nest_cli.cli.cli``. This module is
intentionally thin so that ``pyproject.toml``'s
``nest-cli = "nest_cli.__main__:main"`` keeps working without a re-export
gymnastics, AND so that test code that patches ``nest_cli.__main__:main``
(e.g. ``tests/test_skeleton.py``) does not need to know which submodule
the group lives in.
"""

from __future__ import annotations

from nest_cli.cli import cli as main

__all__ = ["main"]


if __name__ == "__main__":
    main()
