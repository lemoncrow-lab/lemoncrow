"""Review *sources*: the adapters that turn a subject into a persisted revision.

One module per kind of thing that can be reviewed. ``local`` is the working
tree / staged index / commit range on this machine; a pull request or a
document would be a sibling, never a second review model.

Nothing is re-exported here on purpose, matching the parent package: import the
submodule you need from inside the function that needs it, so a CLI that never
opens a review never pays for pygit2.
"""

from __future__ import annotations

__all__: list[str] = []
