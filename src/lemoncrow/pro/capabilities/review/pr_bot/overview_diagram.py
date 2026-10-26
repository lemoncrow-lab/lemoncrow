"""Draw the diagram a model proposed (:class:`overview.DiagramSpec`) as Mermaid, in LemonCrow's own words.

The model gives structure: nodes that name a changed file, a changed symbol or a generic actor, and short edge labels.
Everything that is Mermaid is written here:

* node and subgraph ids are counters (``n1``, ``n2`` ...), never a name from the pull request or the model;
* every label goes through the same escaper the Architecture Delta diagram uses (:func:`diagram._esc`): a quote, bracket,
  angle bracket, pipe, backtick, ``@``, ``#``, ``;`` or ``%`` becomes a numeric entity, so a label can neither close
  itself, start a tag or a comment, mention anyone, nor open a Markdown fence;
* the first line is the neutral theme directive (a sequence diagram's also greys its message text and arrows), so the
  picture reads on GitHub's light and dark pages;
* at most :data:`overview.MAX_NODES` nodes and :data:`overview.MAX_EDGES` edges, which :func:`overview.parse` already held.

A file is drawn as a box, a symbol as a rounded box and an actor as a stadium; a ``flowchart`` has arrows, a ``sequence``
has the messages in the order the edges are listed. The caption under the block says what the picture is: the model's
reading of the diff, with nodes that are all real names, and arrows nobody checked against the code.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Final

from lemoncrow.pro.capabilities.review.pr_bot import diagram as delta_diagram
from lemoncrow.pro.capabilities.review.pr_bot.overview import MAX_LABEL, DiagramSpec, Node
from lemoncrow.pro.capabilities.review.pr_bot.render import fence_for

CAPTION: Final[str] = (
    "<sub>Sketched by the model from the diff. Every box is a changed file, a changed symbol or a generic actor; the "
    "arrows are its reading of the change and are not checked against the code.</sub>"
)
#: Mermaid's node shapes by what the node names.
_SHAPES: Final[dict[str, tuple[str, str]]] = {"file": ("[", "]"), "symbol": ("(", ")"), "actor": ("([", "])")}


def node_text(node: Node, nodes: Sequence[Node]) -> str:
    """What a node says, escaped: a file by its name (its whole path when two files share a name), else the name."""

    name = node.name
    if node.kind == "file":
        base = PurePosixPath(name).name
        shared = sum(1 for other in nodes if other.kind == "file" and PurePosixPath(other.name).name == base) > 1
        name = name if shared else base
    return delta_diagram._label(name, MAX_LABEL, tail=node.kind == "file")


def mermaid(spec: DiagramSpec) -> str:
    """The Mermaid text for *spec*: the theme line, the diagram type, the nodes, then the edges."""

    ids = {node.id: f"n{index}" for index, node in enumerate(spec.nodes, 1)}
    if spec.kind == "sequence":
        lines = [delta_diagram.SEQUENCE_INIT, "sequenceDiagram"]
        lines += [f"    participant {ids[node.id]} as {node_text(node, spec.nodes)}" for node in spec.nodes]
        lines += [
            f"    {ids[edge.source]}->>{ids[edge.target]}: {delta_diagram._label(edge.label, MAX_LABEL)}"
            for edge in spec.edges
        ]
        return "\n".join(lines)
    lines = [delta_diagram.THEME_INIT, "flowchart TD"]
    for node in spec.nodes:
        left, right = _SHAPES[node.kind]
        lines.append(f'    {ids[node.id]}{left}"{node_text(node, spec.nodes)}"{right}')
    for edge in spec.edges:
        label = f'|"{delta_diagram._label(edge.label, MAX_LABEL)}"|' if edge.label else ""
        lines.append(f"    {ids[edge.source]} -->{label} {ids[edge.target]}")
    return "\n".join(lines)


def draw(spec: DiagramSpec) -> delta_diagram.Diagram:
    """The ``### Diagram`` section for *spec*, as the same :class:`diagram.Diagram` the Architecture Delta path makes, so
    the summary treats both alike (the state records its digest, and it is the first thing dropped at the size limit).
    """

    block = mermaid(spec)
    fence = fence_for(block)
    section = "\n".join(
        [
            delta_diagram.SECTION_OPEN,
            "",
            f"{fence}mermaid",
            block,
            fence,
            "",
            CAPTION,
            "",
            delta_diagram.SECTION_CLOSE,
        ]
    )
    return delta_diagram.Diagram(section=section, state=delta_diagram.digest_of(section), kind=spec.kind)


__all__ = ["CAPTION", "draw", "mermaid", "node_text"]
