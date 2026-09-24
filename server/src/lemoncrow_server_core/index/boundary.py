"""The data boundary below the view: which declared digests it may address.

Layer 1 is org-scoped and content-addressed, and stays that way -- the storage
win the plan asks for is exactly that branches, worktrees and developers inside
one enterprise share identical content. What the plan never said, and what the
store therefore did, is answer *"do I need to upload this?"* from whether the
**organization** holds the bytes. Under that rule possessing a digest was
possessing the file: an engineer who learned a digest from a build log could
name it in a manifest for a workspace they legitimately owned and read a
colleague's source out of the shared store, having never held the file and
holding no binding on the repository it came from.

This module is the boundary that fixes it, and it is one rule:

    A digest a view declares is addressable in that view only if the view's own
    content scope has *demonstrated possession* of it -- somebody actually
    handed the server those bytes there and the server hashed them.

Three properties follow, and they are the reason it is shaped this way.

**Declaration writes nothing.** :class:`~.contracts.ContentProvenance` rows are
written on upload and on a verified read from a proven same-host worktree, and
nowhere else. A manifest row is a claim about a path, and a claim is not
evidence.

**There is no existence oracle.** The answer for "the organization holds these
bytes under a scope you cannot reach" is the *same* answer as for "nobody has
ever uploaded this": the digest is not addressable, so it is reported as needed
and the client is asked for the bytes. Neither the missing route, the
manifest-chunk acknowledgement, the ``{need: [...]}`` answer nor a query hit can
tell the two apart, because none of them consults whether the organization holds
anything -- they consult this.

**Deduplication survives.** The scope is the *repository*, not the engineer, for
anyone holding a binding that names the repository as a sharing unit: a team on
one monorepo uploads a blob once between them. Everyone else is scoped to their
own demonstrated possession inside that repository, which costs a first upload
per engineer and costs nothing after it.
"""

from __future__ import annotations

from collections.abc import Sequence

from .contracts import ContentProvenance, ViewState, ViewStore

__all__ = ["addressable", "addressable_in"]


def addressable_in(
    provenance: ContentProvenance,
    state: ViewState,
    digests: Sequence[str],
) -> frozenset[str]:
    """Of ``digests``, those ``state`` may actually read content for.

    Takes the view state rather than an identifier because every caller already
    holds one: the query bound its answer to a revision and the materializer
    proved its tree is revision-exact, so re-reading the view here would be a
    second read of something that must not have moved in between.
    """
    if not digests:
        return frozenset()
    return provenance.held(state.org_id, state.repo_id, state.content_subject, digests)


def addressable(
    views: ViewStore,
    provenance: ContentProvenance,
    org_id: str,
    view_id: str,
    digests: Sequence[str],
) -> frozenset[str]:
    """The same answer for a caller that holds only the view's identifier.

    ``views.get`` refuses another organization's view exactly as it refuses one
    that never existed, so this cannot be used to probe for a workspace either.
    """
    if not digests:
        return frozenset()
    return addressable_in(provenance, views.get(org_id, view_id), digests)
