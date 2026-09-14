"""Self-contained HTML view of a review packet.

Why this module exists: the terminal renderer has one screen and no memory, so
it truncates (ten sites per finding, ten uninspected files) and it cannot let a
reader move. A review of thirty files is *navigation* before it is anything
else -- jump to the file the ranking put first, read what it reaches, jump to
the site that reaches it, come back. That is a document, not a stream, so this
module emits one.

Three constraints shape every decision here:

* **One file, no network.** No CDN, no webfont, no remote image, no ``fetch``.
  The page has to open from a ``file:`` URL on a locked-down laptop, so every
  byte of CSS and JS is inline.
* **Every string is untrusted.** Hunk headers carry the enclosing source line,
  impact snippets carry a slice of somebody's code, and a commit subject is
  whatever a human typed. All of it goes through :func:`_esc`; the only
  unescaped values in the output are integers this module computed itself.
* **The HTML and the terminal must not disagree.** Both renderers ask
  ``review.render`` for the grouping and the ordering, so "one finding, nine
  locations" cannot mean one thing on screen and another in the file.

The page is a superset, never a contradiction: it adds the in-patch impact
sites, the ranking scores and the untruncated uninspected list -- the things
the terminal drops for width, not the things it deliberately refuses to claim.
There is still no verdict anywhere in it.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from dataclasses import replace

from .models import ChangedFile, EvidenceRecord, ImpactSite, ProvenanceRecord, ReviewPacket

# Imported, not re-derived: these private helpers *are* the product decisions
# PR-3 and PR-4 landed (what counts as one finding, which file the reading
# order names first, what an unrecorded read list is allowed to say). A second
# copy here would drift, and a review that says two different things about the
# same change is worse than one that says nothing.
from .render import (
    _HUMAN_REVIEW_FOOTER,
    _STATUS_CHAR,
    _attention_groups,
    _attention_label,
    _file_delta,
    _file_label,
    _footer_line,
    _grouped_files,
    _order_rows,
    _range_label,
    _site_lines,
    _summary_line,
)

_MODE_LABEL = {
    "commit_range": "commit range",
    "working_tree": "working tree",
    "staged": "staged index",
}
_STATUS_WORD = {
    "added": "added",
    "modified": "modified",
    "deleted": "deleted",
    "renamed": "renamed",
    "copied": "copied",
    "typechange": "type change",
}
# Widest hunk bar, in per-cent of its track. Scaled against the largest hunk in
# the same file so the bars answer "which hunk is the big one", not "is this
# repository large".
_BAR_MAX = 100


def _esc(value: object) -> str:
    """Escape anything for an HTML text node or a double-quoted attribute."""

    return html.escape("" if value is None else str(value), quote=True)


def _pct(part: int, whole: int) -> int:
    if whole <= 0 or part <= 0:
        return 0
    return max(1, min(_BAR_MAX, round(_BAR_MAX * part / whole)))


# --- structure --------------------------------------------------------------


def _ordered_files(packet: ReviewPacket) -> list[ChangedFile]:
    """Return the files in reading order: ranked first, then everything else.

    ``--limit`` truncates ``packet.order``, and a file the ranker dropped is
    still a file that changed -- omitting it from the document would make the
    HTML the only surface where a change can vanish.
    """

    by_path = {item.path: item for item in packet.files}
    out: list[ChangedFile] = []
    seen: set[str] = set()
    for entry in packet.order:
        item = by_path.get(entry.path)
        if item is not None and item.path not in seen:
            seen.add(item.path)
            out.append(item)
    for _category, rows in _grouped_files(packet):
        for item in rows:
            if item.path not in seen:
                seen.add(item.path)
                out.append(item)
    return out


def _anchor_map(files: Sequence[ChangedFile]) -> dict[str, str]:
    """Map each path to a synthetic anchor id.

    The id is positional (``f3``) rather than path-derived on purpose: a path is
    user data, and user data does not belong in an ``id``/``href`` pair.
    """

    return {item.path: f"f{index}" for index, item in enumerate(files, start=1)}


def _rank_map(packet: ReviewPacket) -> dict[str, int]:
    return {entry.path: entry.rank for entry in packet.order}


def _path_link(path: str, anchors: dict[str, str], *, label: str | None = None) -> str:
    """Link to a file section when the file is in the diff, else plain text."""

    text = _esc(label if label is not None else path)
    anchor = anchors.get(path)
    if anchor is None:
        return f'<span class="path">{text}</span>'
    return f'<a class="path" href="#{anchor}">{text}</a>'


# --- header -----------------------------------------------------------------


def _chip(value: str, label: str, cls: str = "") -> str:
    css = f"chip {cls}".strip()
    return f'<span class="{css}"><b>{_esc(value)}</b>{_esc(label)}</span>'


def _header_html(packet: ReviewPacket) -> str:
    stats = packet.stats
    chips = [
        _chip(f"{stats.get('files', len(packet.files)):,}", "files"),
        _chip(f"+{stats.get('additions', 0):,}", "added", "add"),
        _chip(f"-{stats.get('deletions', 0):,}", "removed", "del"),
    ]
    if stats.get("hunks"):
        chips.append(_chip(f"{stats['hunks']:,}", "hunks"))
    if stats.get("symbols"):
        chips.append(_chip(f"{stats['symbols']:,}", "symbols"))
    if stats.get("impact_sites"):
        chips.append(_chip(f"{stats['impact_sites']:,}", "impact sites", "warn"))
    if packet.dirty and packet.range_mode == "commit_range":
        chips.append(_chip("dirty", "working tree", "warn"))

    title = packet.title or _range_label(packet)
    mode = _MODE_LABEL.get(packet.range_mode, packet.range_mode)
    return (
        '<header class="top"><div class="topin">'
        '<p class="crumbs"><span class="brand">lc review</span>'
        f'<code class="range">{_esc(_range_label(packet))}</code>'
        f'<span class="mode">{_esc(mode)}</span></p>'
        f"<h1>{_esc(title)}</h1>"
        f'<p class="lede">{_esc(_summary_line(packet))} &middot; no model was run, no verdict is issued &mdash; '
        "this page is evidence for a human reviewer.</p>"
        f'<p class="chips">{"".join(chips)}</p>'
        "</div>"
        '<button id="theme" class="theme" type="button" aria-label="Toggle dark mode">theme</button>'
        "</header>"
    )


# --- review order -----------------------------------------------------------


def _order_html(packet: ReviewPacket, anchors: dict[str, str]) -> str:
    if not packet.order:
        return ""
    rows: list[str] = []
    for (rank, path, facts, _delta, reasons), entry in zip(_order_rows(packet), packet.order, strict=False):
        bullets = "".join(f"<li>{_esc(reason)}</li>" for reason in reasons)
        reason_html = f'<ul class="reasons">{bullets}</ul>' if bullets else ""
        # The score is the sort key, not a verdict, so it rides along quietly:
        # the terminal drops it (the reasons are the argument) and `--json`
        # keeps it. Here it is the tie-breaker a reader can check.
        score = f'<span class="score" title="ranking score">{entry.score:g}</span>'
        rows.append(
            '<li class="orow">'
            f'<span class="rank">{rank}</span>'
            f'<div><p class="oh">{_path_link(path, anchors)}{score}</p>'
            f'<p class="facts">{_esc(facts)}</p>'
            f"{reason_html}</div></li>"
        )
    return (
        '<section class="pane" id="order"><h2>Review order</h2>'
        '<p class="note">Ranked by blast radius, not by path. The reasons are the ranking &mdash; '
        "disagree with one and the order is wrong.</p>"
        f'<ol class="order">{"".join(rows)}</ol></section>'
    )


# --- files ------------------------------------------------------------------


def _hunk_table(item: ChangedFile) -> str:
    if item.is_binary:
        return '<p class="empty">binary file &mdash; no textual patch</p>'
    if not item.hunks:
        return '<p class="empty">no textual hunks in this delta</p>'

    peak = max(hunk.added + hunk.removed for hunk in item.hunks)
    rows: list[str] = []
    for hunk in item.hunks:
        if hunk.new_lines <= 0:
            span = f"L{hunk.old_start} removed"
        elif hunk.new_lines == 1:
            span = f"L{hunk.new_start}"
        else:
            span = f"L{hunk.new_start}-{hunk.new_start + hunk.new_lines - 1}"
        bar = (
            '<span class="bar">'
            f'<i class="a" style="width:{_pct(hunk.added, peak)}%"></i>'
            f'<i class="d" style="width:{_pct(hunk.removed, peak)}%"></i>'
            "</span>"
        )
        rows.append(
            "<tr>"
            f'<td class="ln">{_esc(span)}</td>'
            f'<td class="hh"><code>{_esc(hunk.header)}</code></td>'
            f'<td class="dl">{bar}<i class="a">+{hunk.added:,}</i> <i class="d">-{hunk.removed:,}</i></td>'
            "</tr>"
        )
    head = '<thead><tr><th scope="col">Lines</th><th scope="col">Hunk</th><th scope="col">Change</th></tr></thead>'
    return f'<div class="scroll"><table class="hunks">{head}<tbody>{"".join(rows)}</tbody></table></div>'


def _file_section(item: ChangedFile, anchor: str, rank: int | None, reaches: Sequence[ImpactSite]) -> str:
    meta = [_STATUS_WORD.get(item.status, item.status), item.category]
    if item.language:
        meta.append(item.language)
    if item.similarity:
        meta.append(f"{item.similarity}% similar")
    if item.hunks:
        meta.append(f"{len(item.hunks):,} hunk{'' if len(item.hunks) == 1 else 's'}")

    reach = ""
    if reaches:
        items = "".join(f"<li><code>{_esc(site.path)}</code></li>" for site in reaches)
        reach = (
            '<details class="reach"><summary>reaches '
            f"{len(reaches):,} site{'' if len(reaches) == 1 else 's'} outside this patch</summary>"
            f'<ul class="sites">{items}</ul></details>'
        )

    rank_html = f'<span class="rank">{rank}</span>' if rank is not None else ""
    return (
        f'<section class="file" id="{anchor}" data-path="{_esc(item.path)}">'
        f'<h3>{rank_html}<span class="st s-{_esc(item.status)}">{_esc(_STATUS_CHAR.get(item.status, "?"))}</span>'
        f'<code class="fp">{_esc(_file_label(item))}</code>'
        f'<span class="delta">{_esc(_file_delta(item))}</span></h3>'
        f'<p class="meta">{_esc(" · ".join(meta))}</p>'
        f"{reach}{_hunk_table(item)}"
        "</section>"
    )


def _files_html(packet: ReviewPacket, files: Sequence[ChangedFile], anchors: dict[str, str]) -> str:
    ranks = _rank_map(packet)
    outward: dict[str, list[ImpactSite]] = {}
    for site in packet.impact:
        if not site.in_patch and site.source_path:
            outward.setdefault(site.source_path, []).append(site)
    sections = [
        _file_section(item, anchors[item.path], ranks.get(item.path), outward.get(item.path, ())) for item in files
    ]
    return f'<section class="pane" id="files"><h2>Files</h2>{"".join(sections)}</section>'


# --- attention --------------------------------------------------------------


def _finding_html(
    kind: str,
    headline: str,
    source: str,
    uncertainty: str,
    sites: Sequence[ImpactSite],
    anchors: dict[str, str],
) -> str:
    origin = f'<p class="src">from {_path_link(source, anchors)}</p>' if source else ""
    rows = "".join(
        f"<li><code>{_esc(location)}</code>"
        + (f'<span class="snip">{_esc(context)}</span>' if context else "")
        + "</li>"
        for location, context in _site_lines(sites)
    )
    # A qualified finding gets a different card, icon and label -- the page must not
    # be the one place the doubt is dropped, or the HTML and the terminal disagree
    # about what is a fact.
    icon = "?" if uncertainty else "&#9888;"
    doubt = f'<p class="doubt">{_esc(uncertainty)}</p>' if uncertainty else ""
    return (
        f'<article class="finding{" unsure" if uncertainty else ""}">'
        f'<h3><span class="ic" aria-hidden="true">{icon}</span>{_esc(headline)}'
        f'<span class="kind">{_esc(kind.replace("_", " "))}</span></h3>'
        f"{origin}{doubt}"
        f'<p class="label">{_esc(_attention_label(kind, uncertainty))}</p>'
        f'<ul class="sites">{rows}</ul>'
        "</article>"
    )


def _in_patch_groups(packet: ReviewPacket) -> list[tuple[str, str, str, str, list[ImpactSite]]]:
    """Group the *in-patch* sites with the identical grouping the terminal uses.

    ``_attention_groups`` deliberately drops these -- a site inside the diff is
    already on the reviewer's screen. The page has room for them, so it flips
    the flag and reuses the same grouping rather than writing a second one that
    could disagree about what "one finding" means.
    """

    inside = tuple(replace(site, in_patch=False) for site in packet.impact if site.in_patch)
    if not inside:
        return []
    return _attention_groups(replace(packet, impact=inside))


def _attention_html(packet: ReviewPacket, anchors: dict[str, str]) -> str:
    outside = _attention_groups(packet)
    inside = _in_patch_groups(packet)
    if not outside and not inside:
        return ""

    blocks: list[str] = []
    if outside:
        blocks.append("".join(_finding_html(*group, anchors) for group in outside))
    else:
        blocks.append('<p class="empty">Nothing in this change reaches outside the patch.</p>')
    if inside:
        body = "".join(_finding_html(*group, anchors) for group in inside)
        count = sum(len(group[4]) for group in inside)
        blocks.append(
            '<details class="inpatch"><summary>'
            f"{count:,} impacted site{'' if count == 1 else 's'} inside the patch</summary>{body}</details>"
        )
    return (
        '<section class="pane" id="attention"><h2>Attention</h2>'
        '<p class="note">One finding, every location it reaches. Sites outside the patch come first: '
        "they are the code nobody edited and nobody has read.</p>"
        f'{"".join(blocks)}</section>'
    )


# --- execution evidence -----------------------------------------------------


def _kv(label: str, value: str, *, raw: bool = False) -> str:
    return f"<dt>{_esc(label)}</dt><dd>{value if raw else _esc(value)}</dd>"


def _provenance_html(record: ProvenanceRecord, anchors: dict[str, str]) -> str:
    if record.status == "unknown":
        # R1/A4: no session record stores a commit sha, so correlation is a
        # heuristic. An honest gap beats a confident wrong attribution, and the
        # reason still prints so the gap is auditable.
        rows = [_kv("Generated with", "unknown")]
        if record.match_reason:
            rows.append(_kv("Why", record.match_reason))
        return f'<dl class="kv">{"".join(rows)}</dl>'

    # The same rule the terminal follows: the hedge sits on the line that makes
    # the claim, because "Generated with: claude" three rows above a confidence
    # number reads as a fact to everyone who stops reading at row one.
    badge = (
        ""
        if record.certainty == "exact"
        else f'<span class="badge amb">{_esc(record.certainty)} match &middot; unconfirmed</span>'
    )
    rows = [_kv("Generated with", f"{_esc(record.host or 'unknown')}{badge}", raw=True)]
    if record.model:
        rows.append(_kv("Model", record.model))
    if record.session_id:
        rows.append(_kv("Session", f"<code>{_esc(record.session_id)}</code>", raw=True))
    if record.match_reason:
        rows.append(_kv("Matched on", record.match_reason))
    rows.append(_kv("Confidence", f"{record.match_confidence:.2f} ({record.certainty})"))
    if record.task:
        rows.append(_kv("Task", record.task))
    if record.files_inspected:
        rows.append(_kv("Agent inspected", f"{len(record.files_inspected):,} files"))
    else:
        # R2: several hosts never record reads at all. "0 files" would read as a
        # finding; this is a gap in the recording, not in the agent's work.
        rows.append(_kv("Agent inspected", "not recorded for this host"))
    if record.commands_run:
        rows.append(_kv("Commands run", f"{len(record.commands_run):,}"))
    if record.subagents:
        agents = ", ".join(f"{_esc(name)}&nbsp;&times;{count}" for name, count in record.subagents)
        rows.append(_kv("Subagents", agents, raw=True))

    body = f'<dl class="kv">{"".join(rows)}</dl>'
    if record.commands_run:
        commands = "".join(f"<li><code>{_esc(command)}</code></li>" for command in record.commands_run)
        body += f'<details class="cmds"><summary>commands the agent ran</summary><ul>{commands}</ul></details>'
    if record.uninspected_impacted:
        # Untruncated on purpose: this is the one list a reviewer reads to the
        # end, and the terminal is the only surface that has to cut it short.
        paths = "".join(f"<li>{_path_link(path, anchors)}</li>" for path in record.uninspected_impacted)
        body += (
            '<div class="uninspected"><p class="label">Agent did not inspect '
            f"({len(record.uninspected_impacted):,}):</p>"
            f'<ul class="sites">{paths}</ul></div>'
        )
    return body


def _evidence_html(records: Sequence[EvidenceRecord]) -> str:
    if not records:
        return ""
    rows = "".join(
        "<tr>"
        f"<td>{_esc(record.name)}</td>"
        f'<td><span class="badge st-{_esc(record.status.lower())}">{_esc(record.status)}</span></td>'
        f"<td>{_esc(record.detail)}</td>"
        f'<td class="src">{_esc(record.source)}</td>'
        "</tr>"
        for record in records
    )
    head = (
        "<thead><tr>"
        '<th scope="col">Check</th><th scope="col">Status</th>'
        '<th scope="col">Detail</th><th scope="col">Source</th>'
        "</tr></thead>"
    )
    return f'<div class="scroll"><table class="ev">{head}<tbody>{rows}</tbody></table></div>'


def _evidence_pane(packet: ReviewPacket, anchors: dict[str, str]) -> str:
    return (
        '<section class="pane" id="evidence"><h2>Execution evidence</h2>'
        '<p class="note">What actually ran, and what was never recorded. Nothing here is inferred: '
        "a blank is reported as a blank.</p>"
        f"{_provenance_html(packet.provenance, anchors)}"
        f"{_evidence_html(packet.evidence)}"
        "</section>"
    )


# --- navigation -------------------------------------------------------------


def _nav_html(packet: ReviewPacket, files: Sequence[ChangedFile], anchors: dict[str, str]) -> str:
    jumps = [('<a href="#order">Review order</a>' if packet.order else ""), '<a href="#files">Files</a>']
    outside = sum(1 for site in packet.impact if not site.in_patch)
    if outside:
        jumps.append(f'<a href="#attention">Attention <b>{outside:,}</b></a>')
    jumps.append('<a href="#evidence">Execution evidence</a>')

    ranks = _rank_map(packet)
    rows: list[str] = []
    for item in files:
        rank = ranks.get(item.path)
        # Basename first, directory second and dimmed: a 60-character path
        # wrapped over three lines is a wall, and the reviewer is scanning for
        # the file name they already have in their head.
        head, sep, name = item.path.rpartition("/")
        # The last two directory components, not the whole prefix: enough to
        # tell `review/render.py` from `usage/render.py`, short enough to stay
        # on one line. The full path is in the link title and the section head.
        tail = "/".join(head.split("/")[-2:])
        directory = f'<span class="dir">{_esc(tail)}</span>' if sep else ""
        rows.append(
            f'<li data-path="{_esc(item.path)}">'
            f'<a href="#{anchors[item.path]}" title="{_esc(item.path)}">'
            f'<span class="rank">{rank if rank is not None else "&middot;"}</span>'
            f'<span class="st s-{_esc(item.status)}">{_esc(_STATUS_CHAR.get(item.status, "?"))}</span>'
            f'<span class="nm"><b>{_esc(name)}</b>'
            f'<span class="delta">{_esc(_file_delta(item))}</span></span>'
            f"{directory}"
            "</a></li>"
        )
    return (
        '<nav class="side" aria-label="Review navigation">'
        f'<ul class="jump">{"".join(f"<li>{link}</li>" for link in jumps if link)}</ul>'
        '<div class="filterbox">'
        '<input id="filter" type="search" placeholder="filter files… (/)" aria-label="Filter files" '
        'autocomplete="off" spellcheck="false">'
        '<p class="count" id="count" aria-live="polite"></p>'
        "</div>"
        f'<ol class="filelist">{"".join(rows)}</ol>'
        '<p class="hint"><kbd>j</kbd> / <kbd>k</kbd> move between files</p>'
        "</nav>"
    )


# --- footer -----------------------------------------------------------------


def _footer_html(packet: ReviewPacket) -> str:
    degraded = ""
    if packet.degraded:
        chips = "".join(f'<span class="badge warn">{_esc(name)}</span>' for name in packet.degraded)
        degraded = f'<p class="deg">{chips}</p>'
    status_line = _footer_line(packet)
    status = f'<p class="status">{_esc(status_line)}</p>' if status_line else ""
    # The whole packet, escaped, in a collapsed block: the page is often the
    # only artefact that survives the terminal scrollback, so the machine-
    # readable form has to survive with it.
    payload = json.dumps(packet.to_dict(), indent=2, ensure_ascii=False, default=str)
    return (
        '<footer class="foot">'
        f'<p class="required">{_esc(_HUMAN_REVIEW_FOOTER)}</p>'
        f"{status}"
        f"{degraded}"
        f'<p class="gen">generated {_esc(packet.generated_at)} · <code>{_esc(packet.repo_root)}</code></p>'
        f'<details class="raw"><summary>packet JSON (schema {packet.schema_version})</summary>'
        f"<pre>{_esc(payload)}</pre></details>"
        "</footer>"
    )


# --- document ---------------------------------------------------------------


def render_html(packet: ReviewPacket) -> str:
    """Render *packet* as one self-contained HTML document.

    Everything the page needs is in the returned string: no stylesheet link, no
    script src, no font, no image, no request of any kind. Writing it to a path
    and opening that path is the whole contract.
    """

    files = _ordered_files(packet)
    anchors = _anchor_map(files)
    title = packet.title or _range_label(packet)

    if files:
        body = (
            f"{_nav_html(packet, files, anchors)}"
            "<main>"
            f"{_order_html(packet, anchors)}"
            f"{_attention_html(packet, anchors)}"
            f"{_files_html(packet, files, anchors)}"
            f"{_evidence_pane(packet, anchors)}"
            "</main>"
        )
    else:
        body = f'<main><p class="empty big">no changes in this range</p>{_evidence_pane(packet, anchors)}</main>'

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>lc review — {_esc(title)}</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n"
        f"{_header_html(packet)}"
        f'<div class="page">{body}</div>'
        f"{_footer_html(packet)}"
        f"<script>{_JS}</script>\n</body>\n</html>\n"
    )


__all__ = ["render_html"]


# --- assets -----------------------------------------------------------------

# Light is the default and dark is honoured through `prefers-color-scheme`, so
# the page is readable before any script runs. The `[data-theme]` rules carry
# higher specificity than the bare `:root` rules, so the toggle wins in *both*
# directions -- a viewer on a dark OS can still force light.
_LIGHT = (
    "--bg:#f7f8f9;--surface:#fff;--surface-2:#f1f3f5;--text:#12181d;--muted:#5a6672;--faint:#8b96a1;"
    "--border:#e0e5ea;--accent:#0f62d0;--accent-soft:#e9f1fd;--add:#136f2e;--add-bg:#e7f5ec;"
    "--del:#a8291d;--del-bg:#fdecea;--warn:#8a5a00;--warn-bg:#fbf1da;--warn-border:#e3c37a"
)
_DARK = (
    "--bg:#0d1115;--surface:#141a20;--surface-2:#101720;--text:#dde5ec;--muted:#93a1ae;--faint:#6a7783;"
    "--border:#232d36;--accent:#5aa2ff;--accent-soft:#12203a;--add:#4cc266;--add-bg:#0f2417;"
    "--del:#f0705f;--del-bg:#2a1512;--warn:#e0ac3f;--warn-bg:#2a2210;--warn-border:#5b4a1e"
)

_CSS = (
    ":root{color-scheme:light dark;"
    "--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
    '--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;'
    f"{_LIGHT}}}"
    f"@media(prefers-color-scheme:dark){{:root{{{_DARK}}}}}"
    f":root[data-theme=dark]{{{_DARK}}}"
    f":root[data-theme=light]{{{_LIGHT}}}"
    """
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
a{color:var(--accent)}
code{font-family:var(--mono)}
.top{border-bottom:1px solid var(--border);background:var(--surface)}
.topin{max-width:1180px;margin:0 auto;padding:26px 22px 20px}
.top{display:block;position:relative}
.crumbs{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 6px;font-size:12px}
.brand{font-family:var(--mono);font-weight:700;color:var(--accent);letter-spacing:.02em}
.range{font-family:var(--mono);color:var(--muted);background:var(--surface-2);
  border:1px solid var(--border);border-radius:5px;padding:1px 7px}
.mode{color:var(--faint)}
h1{font-size:23px;line-height:1.25;margin:0;letter-spacing:-.01em;overflow-wrap:anywhere}
.lede{color:var(--muted);font-size:13px;margin:7px 0 0;max-width:78ch}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:13px 0 0}
.chip{font-family:var(--mono);font-size:11.5px;color:var(--muted);background:var(--surface-2);
  border:1px solid var(--border);border-radius:20px;padding:2px 10px}
.chip b{color:var(--text);font-variant-numeric:tabular-nums;margin-right:5px}
.chip.add b{color:var(--add)}.chip.del b{color:var(--del)}
.chip.warn{border-color:var(--warn-border);background:var(--warn-bg)}.chip.warn b{color:var(--warn)}
.theme{position:absolute;top:16px;right:16px;font-family:var(--mono);font-size:11.5px;color:var(--muted);
  background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:4px 10px;cursor:pointer}
.theme:hover{color:var(--text)}
.theme:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.page{max-width:1180px;margin:0 auto;padding:0 22px;display:grid;
  grid-template-columns:262px minmax(0,1fr);gap:30px;align-items:start}
main{min-width:0;padding-bottom:20px}
.side{position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto;padding:20px 0 10px;min-width:0}
.jump{list-style:none;margin:0 0 14px;padding:0;display:flex;flex-direction:column;gap:1px}
.jump a{display:block;padding:4px 8px;border-radius:6px;text-decoration:none;color:var(--muted);
  font-family:var(--mono);font-size:12px}
.jump a:hover{background:var(--surface-2);color:var(--text)}
.jump b{color:var(--warn);font-variant-numeric:tabular-nums}
.filterbox{margin:0 0 8px}
#filter{width:100%;font:12.5px var(--sans);color:var(--text);background:var(--surface);
  border:1px solid var(--border);border-radius:7px;padding:6px 9px}
#filter:focus{outline:2px solid var(--accent);outline-offset:-1px}
.count{margin:5px 2px 0;font-size:11px;color:var(--faint);font-family:var(--mono)}
.filelist{list-style:none;margin:0;padding:0}
.filelist a{display:grid;grid-template-columns:18px 11px minmax(0,1fr);gap:6px;align-items:baseline;
  padding:4px 6px;border-radius:6px;text-decoration:none;color:var(--text)}
.filelist a:hover{background:var(--surface-2)}
.filelist .nm{grid-column:3;display:flex;gap:8px;justify-content:space-between;align-items:baseline}
.filelist .nm b{font:600 12px var(--mono);overflow-wrap:anywhere}
.filelist .dir{grid-column:3;display:block;font-family:var(--mono);font-size:10.5px;color:var(--faint);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.filelist .delta{font-family:var(--mono);font-size:10.5px;color:var(--faint);white-space:nowrap}
.rank{color:var(--faint);font-family:var(--mono);font-size:11px;text-align:right;font-variant-numeric:tabular-nums}
.st{font-family:var(--mono);font-size:11px;font-weight:700;color:var(--muted)}
.s-added{color:var(--add)}.s-deleted{color:var(--del)}.s-modified{color:var(--accent)}
.s-renamed,.s-copied{color:var(--warn)}
.hint{margin:12px 2px;font-size:11px;color:var(--faint)}
kbd{font-family:var(--mono);font-size:10.5px;border:1px solid var(--border);border-bottom-width:2px;
  border-radius:4px;padding:0 4px;background:var(--surface-2)}
.pane{margin:26px 0 34px}
.pane h2{font:600 11.5px var(--mono);text-transform:uppercase;letter-spacing:.09em;color:var(--faint);
  margin:0;padding-bottom:7px;border-bottom:1px solid var(--border)}
.note{color:var(--muted);font-size:12.5px;margin:9px 0 14px;max-width:78ch}
.order{list-style:none;margin:0;padding:0}
.orow{display:grid;grid-template-columns:26px minmax(0,1fr);gap:12px;padding:11px 0;
  border-bottom:1px solid var(--border)}
.orow>.rank{font-size:15px;font-weight:650;color:var(--accent);text-align:right}
.oh{display:flex;justify-content:space-between;gap:12px;align-items:baseline;margin:0}
.path{font-family:var(--mono);font-size:13px;color:var(--text);overflow-wrap:anywhere}
a.path{text-decoration:none;border-bottom:1px solid var(--border)}
a.path:hover{color:var(--accent);border-bottom-color:var(--accent)}
.score{font-family:var(--mono);font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums}
.facts{margin:3px 0 0;color:var(--muted);font-size:12px;font-family:var(--mono)}
.reasons{margin:7px 0 0;padding-left:17px;font-size:13px}
.reasons li{margin:1px 0}
section.file{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:12px 14px;margin:12px 0;scroll-margin-top:14px}
section.file:target{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
section.file h3{display:flex;gap:9px;align-items:baseline;margin:0;font-size:13.5px;font-weight:600}
.fp{font-family:var(--mono);flex:1 1 auto;overflow-wrap:anywhere}
section.file .delta{font-family:var(--mono);font-size:11.5px;color:var(--muted);white-space:nowrap}
.meta{margin:4px 0 9px;color:var(--faint);font-size:11.5px;font-family:var(--mono)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th{text-align:left;font:600 10.5px var(--mono);text-transform:uppercase;letter-spacing:.06em;
  color:var(--faint);border-bottom:1px solid var(--border);padding:4px 9px 5px}
td{padding:5px 9px;border-bottom:1px solid var(--border);vertical-align:top}
tbody tr:last-child td{border-bottom:none}
td.ln{white-space:nowrap;color:var(--muted);font-family:var(--mono)}
td.hh code{white-space:pre;font-size:12px}
td.dl{white-space:nowrap;font-family:var(--mono);font-size:11.5px}
i.a{font-style:normal;color:var(--add)}i.d{font-style:normal;color:var(--del)}
.bar{display:inline-flex;width:92px;height:8px;background:var(--surface-2);border-radius:3px;
  overflow:hidden;margin-right:9px;vertical-align:middle}
.bar i{height:100%}.bar i.a{background:var(--add)}.bar i.d{background:var(--del)}
.finding{border:1px solid var(--warn-border);background:var(--warn-bg);border-radius:10px;
  padding:11px 14px;margin:10px 0}
.finding h3{display:flex;gap:9px;align-items:baseline;flex-wrap:wrap;margin:0;font-size:14px;
  overflow-wrap:anywhere}
.finding .ic{color:var(--warn)}
.finding.unsure{border-style:dashed;background:none}
.finding.unsure .ic{color:var(--muted)}
.doubt{margin:7px 0 1px;font-size:12px;color:var(--muted);font-style:italic;max-width:78ch}
.kind{margin-left:auto;font-family:var(--mono);font-size:10px;color:var(--warn);
  border:1px solid var(--warn-border);border-radius:20px;padding:1px 8px;white-space:nowrap}
.src,.label{margin:7px 0 1px;font-size:12px;color:var(--muted)}
ul.sites{list-style:none;margin:3px 0 0;padding:0}
ul.sites li{display:flex;gap:12px;padding:2px 0;font-size:12px;font-family:var(--mono);
  overflow-wrap:anywhere}
ul.sites .snip{color:var(--muted)}
details{margin:8px 0 0}
summary{cursor:pointer;color:var(--muted);font-size:12px;font-family:var(--mono)}
summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
details.cmds ul{margin:6px 0 0;padding-left:18px;font-size:12px;overflow-wrap:anywhere}
.uninspected{margin-top:12px}
dl.kv{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:5px 18px;margin:12px 0 0}
dl.kv dt{font-family:var(--mono);font-size:11.5px;color:var(--faint)}
dl.kv dd{margin:0;font-size:13px;overflow-wrap:anywhere}
.badge{font-family:var(--mono);font-size:10.5px;font-weight:600;border:1px solid var(--border);
  border-radius:5px;padding:1px 7px;color:var(--muted);background:var(--surface-2)}
.badge.st-pass{color:var(--add);border-color:var(--add);background:var(--add-bg)}
.badge.st-fail{color:var(--del);border-color:var(--del);background:var(--del-bg)}
.badge.amb,.badge.warn{color:var(--warn);border-color:var(--warn-border);background:var(--warn-bg)}
.badge.amb{margin-left:8px}
table.ev{margin-top:16px}
table.ev td.src{color:var(--faint);font-family:var(--mono);font-size:11.5px}
.empty{color:var(--faint);font-size:12.5px;margin:8px 0}
.empty.big{font-size:16px;padding:44px 0;text-align:center}
.foot{max-width:1180px;margin:0 auto;padding:20px 22px 64px;border-top:1px solid var(--border)}
.required{font-family:var(--mono);font-weight:700;letter-spacing:.03em;margin:0 0 8px}
.status,.gen{margin:4px 0;color:var(--muted);font-size:12px;font-family:var(--mono);overflow-wrap:anywhere}
.deg{display:flex;gap:6px;flex-wrap:wrap;margin:9px 0}
.raw pre{max-height:360px;overflow:auto;margin:8px 0 0;padding:11px;border-radius:8px;
  background:var(--surface-2);border:1px solid var(--border);font-size:11px;line-height:1.45;
  white-space:pre-wrap;overflow-wrap:anywhere}
@media(max-width:900px){
  .page{grid-template-columns:minmax(0,1fr);gap:0}
  .side{position:static;max-height:none;border-bottom:1px solid var(--border);padding-bottom:16px}
  .theme{position:static;margin:14px 22px 0}
  .top{display:flex;flex-direction:column;align-items:flex-start}
}
@media(prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
"""
)

# No `fetch`, no storage, no timers: filtering, theming and j/k movement only.
# Every section is already in the DOM, so the page is complete with JS disabled.
_JS = """
(function(){
  var root=document.documentElement;
  var btn=document.getElementById('theme');
  if(btn){btn.addEventListener('click',function(){
    var dark=root.dataset.theme?root.dataset.theme==='dark':
      (window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches);
    root.dataset.theme=dark?'light':'dark';
  });}
  var box=document.getElementById('filter');
  var count=document.getElementById('count');
  var navRows=Array.prototype.slice.call(document.querySelectorAll('.filelist>li'));
  var files=Array.prototype.slice.call(document.querySelectorAll('section.file'));
  var total=files.length;
  function hit(el,needle){
    return !needle||(el.getAttribute('data-path')||'').toLowerCase().indexOf(needle)>=0;
  }
  function apply(){
    var needle=((box&&box.value)||'').trim().toLowerCase();
    var shown=0;
    navRows.forEach(function(el){el.hidden=!hit(el,needle);});
    files.forEach(function(el){var ok=hit(el,needle);el.hidden=!ok;if(ok){shown++;}});
    if(count){count.textContent=needle?shown+' of '+total+' files':
      total+(total===1?' file':' files');}
  }
  if(box){box.addEventListener('input',apply);}
  apply();
  function visible(){return files.filter(function(el){return !el.hidden;});}
  document.addEventListener('keydown',function(ev){
    var tag=((ev.target&&ev.target.tagName)||'').toLowerCase();
    if(ev.metaKey||ev.ctrlKey||ev.altKey){return;}
    if(tag==='input'||tag==='textarea'){
      if(ev.key==='Escape'&&box){box.value='';apply();box.blur();}
      return;
    }
    if(ev.key==='/'){ev.preventDefault();if(box){box.focus();}return;}
    if(ev.key!=='j'&&ev.key!=='k'){return;}
    var list=visible();
    if(!list.length){return;}
    var at=0;
    for(var i=0;i<list.length;i++){
      if(list[i].getBoundingClientRect().top<=60){at=i;}
    }
    at=ev.key==='j'?Math.min(at+1,list.length-1):Math.max(at-1,0);
    ev.preventDefault();
    list[at].scrollIntoView({block:'start'});
  });
})();
"""
