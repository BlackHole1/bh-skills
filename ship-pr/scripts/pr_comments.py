#!/usr/bin/env python3
"""pr_comments — fetch a PR's review threads (and the reviewer-bot summary),
strip the noise that bots like CodeRabbit bury their findings in, and print
clean Markdown — so the agent reads triage-ready content instead of spending
tokens parsing raw GraphQL JSON full of HTML comments, base64 state blobs, and
duplicated <details> blocks.

What it removes from each *bot* comment body:
  - every HTML comment <!-- ... --> (fingerprints, cr-comment ids, the giant
    base64 "internal state" blob), even an unterminated/truncated trailing one
  - the duplicated / verbose collapsible sections (📝 Committable suggestion,
    🤖 Prompt for AI Agents, 🧰 Tools / 🪛 linter dumps, Finishing Touches,
    tips), including everything nested inside them
What it keeps: the finding prose, the path:line, a single suggested-fix diff,
"Also applies to" lines, and the reply-to id you dispute false positives
against. Human comments keep their full text (only the bot's own-line
HTML-comment chrome is stripped; an inline `<!--` in prose or a code span is
left verbatim) — the noise-dropping and <details> rewriting are reviewer-bot
chrome, applied only to bot authors. Content inside fenced ``` / ~~~ code
blocks is preserved verbatim (a reviewer quoting HTML or a diff is never
mangled), using CommonMark-style fence matching (closing fence same char,
length >= opener; unclosed fences protect nothing). What it computes for you
(so the agent doesn't): per-finding severity / type / quick-win tags and a
header tally (unresolved, resolved, by-severity counts, and the "Actionable
comments posted: N" verdict).

Usage: pr_comments.py [PR_NUMBER] [OWNER/REPO] [flags]
  --json         emit structured JSON instead of Markdown
  --all          include resolved threads (default: unresolved only, + a count)
  --no-summary   skip the reviewer-bot PR-level summary block
  --full         keep every <details> section (only strips HTML comments)

  PR_NUMBER  defaults to the current branch's PR (must run inside the repo)
  OWNER/REPO defaults to $GH_REPO, else the repo of the current directory

Testing seam: set PRC_THREADS_FILE / PRC_SUMMARY_FILE to read saved JSON
instead of calling gh (lets the cleaning be unit-tested offline).

Requires: gh (authenticated). Python 3.9+, stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from typing import Any, Dict, List, Optional

from gh_retry import force_utf8, gh_retry, run_command

# Sentinel for "the shell's jq would have failed to parse this input".
_INVALID = object()

# Same GraphQL argv the shell passed to gh api graphql (query string verbatim).
_THREADS_QUERY = (
    "\n"
    "  query($owner:String!,$repo:String!,$pr:Int!){\n"
    "    repository(owner:$owner,name:$repo){\n"
    "      pullRequest(number:$pr){\n"
    "        reviewThreads(first:100){nodes{\n"
    "          isResolved isOutdated path line startLine\n"
    "          comments(first:50){nodes{ databaseId author{login} createdAt body }}\n"
    "        }}\n"
    "      }\n"
    "    }\n"
    "  }"
)

# ---------------------------------------------------------------------------
# jq-compatible primitives. The original cleaner was a 200-line jq program;
# these helpers reproduce its Oniguruma/jq semantics exactly (ascii_downcase,
# truthiness, `//`, string interpolation), because the output is differential-
# tested byte-for-byte against the shell version.
# ---------------------------------------------------------------------------

_ASCII_DOWN = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


def ascii_downcase(s: str) -> str:
    """jq's ascii_downcase lowers ONLY A-Z (Python .lower() also lowers
    non-ASCII letters, which would change emoji-adjacent text like 'İ')."""
    return s.translate(_ASCII_DOWN)


def _truthy(v: Any) -> bool:
    """jq truthiness: only false and null are falsy (0, "", [] are truthy)."""
    return not (v is None or v is False)


def _alt(v: Any, default: Any) -> Any:
    """jq's // operator: fall back when the value is null or false."""
    return default if (v is None or v is False) else v


def _interp(v: Any) -> str:
    """jq string interpolation \\(x): null -> "null", booleans lowercase,
    numbers plain, strings verbatim.

    Known, accepted gap: jq renders a non-scalar as compact JSON (\\(["a"]) is
    '["a"]') where this falls back to Python's str() ("['a']"). Only a
    malformed payload can put an array/object where the renderer interpolates
    (path, line, author, reply_to), so the fidelity is not worth emulating jq's
    whole interpolation grammar — noted instead of built."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


def _dig(obj: Any, *keys: str) -> Any:
    """jq path navigation `.a.b`: indexing null yields null; indexing a
    non-object is an error (raised, caught as 'failed to parse')."""
    cur = obj
    for k in keys:
        if cur is None:
            return None
        if not isinstance(cur, dict):
            raise TypeError("cannot index {} with {!r}".format(type(cur).__name__, k))
        cur = cur.get(k)
    return cur


def _strip_ws(s: str) -> str:
    """jq gsub("^\\s+|\\s+$";"") — strip leading/trailing whitespace."""
    return re.sub(r"^\s+|\s+$", "", s)


def json_documents(text: str) -> Optional[List[Any]]:
    """Decode a CONCATENATED JSON stream into the documents it holds, or None
    when the text is not a valid stream.

    Everywhere the shell piped raw text into jq, jq read a document *stream*,
    not one document — and `gh api ... --paginate > f.json`, the obvious way to
    save a payload for the PRC_* seams, writes exactly such a stream (one
    document per page). A strict json.loads would reject a file the live
    --paginate path handles fine, so both seams decode through here."""
    # jq skips a leading UTF-8 BOM; raw_decode would choke on it and report the
    # whole stream unparseable, which downgrades a real answer into the "failed
    # to parse review threads" report. Same rule as gh_retry.loads_json, applied
    # to the stream's first byte only.
    if text.startswith("\ufeff"):
        text = text[1:]
    dec = json.JSONDecoder()
    docs: List[Any] = []
    i, n = 0, len(text)
    while True:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            return docs
        try:
            val, i = dec.raw_decode(text, i)
        except ValueError:
            return None
        docs.append(val)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Collapsible sections of a *bot* body we drop wholesale (with everything
# nested inside them): pure duplication or verbose dumps. Applied only to bot
# authors. Matched against the lowercased <summary> label and anchored at the
# *start* of the label (CodeRabbit leads each with its marker), so a legit
# section whose title merely contains a phrase like "how to simplify code" is
# never dropped.
NOISE_RE = re.compile(
    "^\\s*(🪛|🧰)"  # 🧰 Tools / 🪛 <linter> dumps (any sub-name)
    "|^\\s*(📝|🤖|🧩|🧠|🚥|✨)?\\s*(committable suggestion|prompt for ai agents"
    "|tools|finishing touch(es)?|simplify code|pre-merge|analysis chain"
    "|outside diff range)\\b"
    "|^\\s*(💡\\s*)?tips\\s*$"
)

# A login is a reviewer bot iff it is GitHub-App-suffixed (slug[bot]) or
# exactly one of the known reviewer slugs (CodeRabbit posts review comments as
# the bare "coderabbitai"). Anchored so a human login that merely *contains* a
# keyword (e.g. "cursorfan", "precursor") is never misread as a bot.
_BOT_SUFFIX_RE = re.compile(r"\[bot\]$")
_BOT_EXACT_RE = re.compile(
    r"^(coderabbitai|coderabbit|sourcery|sourceryai|codium|codiumai"
    r"|qodo|greptile|ellipsis|bugbot|cursor)$"
)

# The PR-level summary uses a looser, *substring* bot list on purpose — it
# mirrors the shell's second jq program exactly and intentionally differs from
# is_bot (summary comments may come from e.g. "sourcery-ai" without a [bot]
# suffix). Keep both lists; do not "unify" them.
_SUMMARY_BOT_RE = re.compile(r"coderabbit|sourcery|qodo|greptile|ellipsis")


def is_bot(login: Optional[str]) -> bool:
    l = ascii_downcase(_alt(login, ""))
    return bool(_BOT_SUFFIX_RE.search(l)) or bool(_BOT_EXACT_RE.search(l))


# ---------------------------------------------------------------------------
# Body cleaning
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_NONSPACE_RE = re.compile(r"[^\s]")
_SUMMARY_LINE_RE = re.compile(r"^\s*(<details[^>]*>)?<summary>.*</summary>\s*$")
_DETAILS_OPEN_RE = re.compile(r"^\s*<details[^>]*>\s*$")
_DETAILS_CLOSE_RE = re.compile(r"^\s*</details>\s*$")
_SUMMARY_LABEL_RE = re.compile(r"<summary>\s*(.*?)\s*</summary>")


def clean(body: str, bot: bool, full: bool = False) -> str:
    """Strip a body down to triage-ready Markdown via a two-pass, fence- and
    nesting-aware state machine (no flat sentinels, so unclosed fences and
    orphan tags can never swallow real signal).

    bot  : when True, drop NOISE <details> subtrees and unwrap other <summary>
           labels to **bold**; when False (human), leave all <details>/<summary>
           markup untouched. Own-line HTML comments are stripped for everyone;
           inline "<!--" (prose or code span) is kept.
    full : --full disables the NOISE-dropping and summary-unwrapping for bot
           and human alike (own-line HTML comments are still stripped, fences
           still honored) — "keep every <details> section".
    """
    lines = body.split("\n")
    n = len(lines)

    # --- Pass 1: matched fence pairs (CommonMark-ish: opener of N backticks/
    #     tildes closes only on a bare marker of the same char and length >= N;
    #     an unclosed opener protects nothing). prot -> line is inside a fence.
    prot = set()
    open_i: Optional[int] = None
    open_f = ""
    for i in range(n):
        m = _FENCE_RE.match(lines[i])
        if m is None:
            continue
        f, rest = m.group(1), m.group(2)
        if open_i is None:
            # opener — but a backtick fence info string may not contain backticks
            if f[0] == "`" and "`" in rest:
                continue
            open_i, open_f = i, f
        elif (
            f[0] == open_f[0]
            and len(f) >= len(open_f)
            and not _NONSPACE_RE.search(rest)
        ):
            prot.update(range(open_i, i + 1))
            open_i = None

    # --- Pass 2: emit cleaned lines. dropdepth/depth track <details> nesting
    #     so a NOISE section drops its whole subtree; incmt eats multi-line
    #     HTML comments.
    out: List[str] = []
    incmt = False
    dropdepth = -1
    depth = 0
    details_aware = bot and not full
    for i in range(n):
        line = lines[i]
        # Eating a multi-line HTML comment takes precedence over fence
        # protection: keep consuming until "-->", then preserve any real text
        # after it.
        if incmt:
            if "-->" in line:
                incmt = False
                tail = re.sub(r"^.*?-->", "", line, count=1)
                if dropdepth < 0 and _NONSPACE_RE.search(tail):
                    out.append(tail)
            continue
        if i in prot:
            if dropdepth < 0:
                out.append(line)
            continue
        # Strip an HTML comment only when it OWNS the line (after optional
        # whitespace) — that is how reviewer bots emit their state/fingerprint
        # chrome. An inline "<!--" in prose, or a `<!--` code span, is the
        # author text and is left verbatim (so it never eats real signal).
        cmt = re.match(r"\s*<!--", line) is not None
        l1 = re.sub(r"<!--.*?-->", "", line) if cmt else line
        if cmt and "<!--" in l1:
            incmt = True
        l2 = re.sub(r"<!--.*$", "", l1) if cmt else line
        # A <summary> opens a labelled section at the current depth. Match it
        # whether on its own line or combined as "<details><summary>…</summary>".
        is_summary = _SUMMARY_LINE_RE.search(line) is not None
        is_open = _DETAILS_OPEN_RE.search(line) is not None
        is_close = _DETAILS_CLOSE_RE.search(line) is not None
        if details_aware and is_summary:
            if re.match(r"\s*<details", line):
                depth += 1
            lbl_m = _SUMMARY_LABEL_RE.search(line)
            lbl = _strip_ws(re.sub(r"[*`]", "", lbl_m.group(1)))
            if dropdepth < 0 and NOISE_RE.search(ascii_downcase(lbl)):
                dropdepth = depth
            elif dropdepth < 0 and lbl != "":
                out.extend(["", "**" + lbl + "**", ""])
        elif details_aware and is_open:
            depth += 1
        elif details_aware and is_close:
            depth = max(depth - 1, 0)
            if 0 <= dropdepth and depth < dropdepth:
                dropdepth = -1
        else:
            if dropdepth < 0:
                out.append(l2)

    s = "\n".join(out)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return _strip_ws(s)


def strip_comments(body: str) -> str:
    """HTML-comment strip used only for *metadata* extraction (tag header /
    title) so a leading "<!-- auto-generated by CodeRabbit -->" line cannot
    hide the tag header. jq flag "m" means DOTALL (dot matches newline)."""
    return re.sub(r"<!--(.*?-->|.*)", "", body, flags=re.DOTALL)


# ---------------------------------------------------------------------------
# Metadata extraction (tag header, severity, title)
# ---------------------------------------------------------------------------

_TAG_HEADER_RE = re.compile(r"^\s*_[^_|]+_(\s*\|\s*_[^_|]+_)*\s*$")


def is_tag_header(l: str) -> bool:
    """Is this line the bot tag header, e.g. "_⚠️ Potential issue_ | _🟠 Major_"?
    Only underscore-delimited tokens joined by "|", each carrying a
    symbol/emoji. The strictness (no prose between tokens) keeps a human line
    like "_note_ and _ok_ 🔧" from being mistaken for a header and stripped.
    Only ever applied to bot authors. jq's \\p{So} has no Python re
    equivalent — check the Unicode category per character instead. Accepted
    limit of that technique: the interpreter's Unicode table can be older than
    the one Oniguruma was compiled against, so a very new symbol may classify
    differently between Python versions (and against jq). There is no stdlib
    way to pin a Unicode version, so this is recorded, not fixed."""
    return bool(_TAG_HEADER_RE.search(l)) and any(
        unicodedata.category(ch) == "So" for ch in l
    )


def _nonblank_lines(body: str) -> List[str]:
    return [l for l in body.split("\n") if _NONSPACE_RE.search(l)]


def header_tags(body: str) -> List[str]:
    first = (_nonblank_lines(body) or [""])[0]
    if not is_tag_header(first):
        return []
    return [
        _strip_ws(re.sub(r"^_|_$", "", m)) for m in re.findall(r"_[^_]+_", first)
    ]


def sev_text(t: str) -> str:
    """Severity from structured tag text (emoji or word)."""
    x = ascii_downcase(t)
    if re.search("critical|🔴", x):
        return "critical"
    if re.search("major|🟠", x):
        return "major"
    if re.search("minor|🟡|🔵", x):
        return "minor"
    if re.search("nitpick|🧹", x):
        return "nitpick"
    return "other"


def sev_of(tags: List[str]) -> str:
    return sev_text(" ".join(tags))


def sev_first(body: str) -> str:
    """Fallback for bots that lead with a markdown heading ("## 🔴 Logic
    Error") instead of an italic tag line: classify by the severity emoji on
    the first non-empty line only (emoji, not words, to avoid prose false
    positives)."""
    l = (_nonblank_lines(body) or [""])[0]
    if "🔴" in l:
        return "critical"
    if "🟠" in l:
        return "major"
    if "🟡" in l or "🔵" in l:
        return "minor"
    if "🧹" in l:
        return "nitpick"
    return "other"


_SEV_RANK = {"critical": 0, "major": 1, "minor": 2, "nitpick": 3, "other": 4}


def sev_rank(s: str) -> int:
    return _SEV_RANK.get(s, 5)


def title_of(body: str) -> str:
    """First **bold** line, else first line; sans markup."""
    lines = _nonblank_lines(body)
    bold = [l for l in lines if "**" in l]
    t = bold[0] if bold else (lines[0] if lines else "")
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("**", "")
    t = re.sub(r"^#+\s*", "", t)
    return _strip_ws(t)


def strip_header_line(body: str) -> str:
    """Drop the "_…_ | _…_" line from displayed prose."""
    lines = body.split("\n")
    if len(lines) > 0 and is_tag_header(lines[0]):
        lines = lines[1:]
    return _strip_ws("\n".join(lines))


# ---------------------------------------------------------------------------
# Thread assembly
# ---------------------------------------------------------------------------


def _thread(node: Any, full: bool) -> Dict[str, Any]:
    cs = _alt(_dig(node, "comments", "nodes"), [])
    if not isinstance(cs, list):
        raise TypeError("comments.nodes is not an array")
    root = _alt(cs[0], {}) if len(cs) > 0 else {}
    rb = _alt(_dig(root, "body"), "")
    root_is_bot = is_bot(_dig(root, "author", "login"))
    rb_meta = strip_comments(rb)
    root_clean = clean(rb, root_is_bot, full)
    tags = header_tags(rb_meta) if root_is_bot else []
    if len(tags) > 0:
        severity = sev_of(tags)
    elif root_is_bot:
        severity = sev_first(rb_meta)
    else:
        severity = "other"
    comments = []
    for c in cs:
        cbot = is_bot(_dig(c, "author", "login"))
        cbody = clean(_alt(_dig(c, "body"), ""), cbot, full)
        if cbot:
            cbody = strip_header_line(cbody)
        comments.append({"author": _alt(_dig(c, "author", "login"), "?"), "body": cbody})
    return {
        "resolved": _dig(node, "isResolved"),
        "outdated": _dig(node, "isOutdated"),
        "path": _dig(node, "path"),
        "line": _alt(_dig(node, "line"), _dig(node, "startLine")),
        "reply_to": _dig(root, "databaseId"),
        "root_author": _alt(_dig(root, "author", "login"), "?"),
        "is_bot": root_is_bot,
        "tags": tags,
        "severity": severity,
        # "quick win" is a substring search over the lowercased tags
        "quick_win": any("quick win" in ascii_downcase(t) for t in tags),
        "title": title_of(root_clean),
        "comments": comments,
    }


def is_failed_fetch(docs: List[Any]) -> bool:
    """Did this threads read fail (GraphQL error page, or a null/absent
    payload)? Per document the test is the shell's: a truthy `errors`, or
    `data == null`.

    The QUANTIFIER is deliberately STRICTER than the shell. `jq -e` ran the gate
    once per document and took its exit status from the LAST output, so a
    concatenated stream whose final page happened to be healthy passed the gate
    even when an earlier page was a GraphQL error — and that page's null payload
    contributes zero threads, so a partial fetch renders as a confident
    "0 unresolved". That is precisely the stale read this gate exists to stop: a
    babysitting agent green-lights a merge on it. Failing closed on a safety
    gate beats reproducing an accident of jq's exit-status rule, so ANY
    unhealthy document fails the whole read; only an all-healthy stream may be
    merged.

    Documents that are not objects at all (a bare string, an array) are not
    judged here — they are not shaped like the query's response, and
    merge_threads / build_data report those as "failed to parse" instead."""
    for doc in docs:
        if doc is None:
            return True
        if isinstance(doc, dict) and (
            _truthy(doc.get("errors")) or doc.get("data") is None
        ):
            return True
    return False


def merge_threads(docs: List[Any]) -> Any:
    """Fold a CONCATENATED stream of threads payloads into the one payload
    build_data consumes. A single document — the live gh api graphql read and
    every hand-saved fixture — is returned untouched, so the common case is
    unchanged. Several documents are what a `--paginate`d save writes (one full
    envelope per page): concatenate their reviewThreads nodes, because keeping
    only one page would under-report unresolved threads, exactly the stale-read
    dishonesty the fetch-failed gate exists to prevent. Returns _INVALID when a
    document is not shaped like the query's response (jq would have errored).

    A page with no payload folds in as zero nodes rather than erroring, but that
    tolerance is only here to keep this fold total: main() runs is_failed_fetch
    over the stream first, so a stream carrying such a page is reported as a
    failed fetch and never reaches this function."""
    if len(docs) == 1:
        return docs[0]
    nodes: List[Any] = []
    for doc in docs:
        try:
            page = _alt(
                _dig(doc, "data", "repository", "pullRequest", "reviewThreads", "nodes"),
                [],
            )
        except TypeError:
            return _INVALID
        if not isinstance(page, list):
            return _INVALID
        nodes.extend(page)
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}


def build_data(
    threads_obj: Any, pr_val: Any, repo_str: str, full: bool
) -> Optional[Dict[str, Any]]:
    """Assemble the result dict (same key order the jq program built). Returns
    None when the payload shape is not what the GraphQL query produces — the
    jq program would have errored out, which the shell reported as "failed to
    parse review threads"."""
    try:
        nodes = _alt(
            _dig(threads_obj, "data", "repository", "pullRequest", "reviewThreads", "nodes"),
            [],
        )
        if not isinstance(nodes, list):
            raise TypeError("nodes is not an array")
        all_threads = [_thread(node, full) for node in nodes]
    except (TypeError, AttributeError, KeyError):
        return None
    open_t = [t for t in all_threads if not _truthy(t["resolved"])]
    done_t = [t for t in all_threads if _truthy(t["resolved"])]
    by_severity: Dict[str, int] = {}
    for s in ("critical", "major", "minor", "nitpick", "other"):
        c = sum(1 for t in open_t if t["severity"] == s)
        if c > 0:
            by_severity[s] = c
    return {
        "pr": pr_val,
        "repo": repo_str,
        "threads_total": len(all_threads),
        "unresolved": len(open_t),
        "resolved": len(done_t),
        "by_severity": by_severity,
        "findings": sorted(open_t, key=lambda t: sev_rank(t["severity"])),  # stable
        "resolved_findings": done_t,
    }


# ---------------------------------------------------------------------------
# Reviewer-bot summary verdict (actionable count)
# ---------------------------------------------------------------------------


def merge_paginated(text: str) -> List[Any]:
    """gh api --paginate emits CONCATENATED JSON arrays (one per page), not a
    single document; the shell merged them with `jq -s 'add // []'`. Decode
    document by document and concatenate; anything empty or malformed degrades
    to [] exactly like the shell's `2>/dev/null || true`."""
    docs = json_documents(text)
    if docs is None:
        return []
    out: List[Any] = []
    for doc in docs:
        if not isinstance(doc, list):
            return []
        out.extend(doc)
    return out


def extract_verdict(summary: Any) -> str:
    """The reviewer bot keeps one PR-level issue comment updated; take bot
    bodies newest-first and report "Actionable comments posted: N" (or the
    no-actionable sentence). Any jq-level error (non-string body, unexpected
    shape) yielded an empty verdict in the shell — mirror that."""
    if isinstance(summary, dict):
        items = list(summary.values())  # jq map() iterates object values
    elif isinstance(summary, list):
        items = summary
    else:
        return ""
    try:
        bodies = []
        for c in items:
            l = ascii_downcase(_alt(_dig(c, "user", "login"), ""))
            if _BOT_SUFFIX_RE.search(l) or _SUMMARY_BOT_RE.search(l):
                bodies.append(_dig(c, "body"))
        bodies.reverse()  # newest first — the bot's latest verdict wins
        first_n: Optional[str] = None
        for b in bodies:
            m = re.search(r"Actionable comments posted:\s*([0-9]+)", b, re.IGNORECASE)
            if m and first_n is None:
                first_n = m.group(1)
        if first_n is not None:
            return "Actionable comments posted: " + first_n
        if any(
            re.search("No actionable comments were generated", b, re.IGNORECASE)
            for b in bodies
        ):
            return "No actionable comments were generated"
        return ""
    except (TypeError, AttributeError):
        return ""


# ---------------------------------------------------------------------------
# Markdown rendering (replicates the shell's jq -r program line for line)
# ---------------------------------------------------------------------------

_SEV_ICON = {"critical": "🔴", "major": "🟠", "minor": "🟡", "nitpick": "🧹", "other": "•"}


def render_markdown(data: Dict[str, Any], verdict: str, want_all: bool) -> str:
    del want_all  # see the resolved-block note below
    em: List[str] = []
    em.append(
        "# Review comments — PR #{}  ({})".format(_interp(data["pr"]), _interp(data["repo"]))
    )
    em.append("")
    counts = [
        "{} unresolved".format(_interp(data["unresolved"])),
        "{} resolved".format(_interp(data["resolved"])),
    ]
    if verdict != "":
        counts.append(verdict)
    em.append(" · ".join(counts))
    if len(data["by_severity"]) > 0:
        em.append(
            "severity: "
            + " · ".join(
                "{} {}".format(k, _interp(v)) for k, v in data["by_severity"].items()
            )
        )
    em.append("")
    if len(data["findings"]) == 0:
        em.append("_No unresolved review threads._")
    else:
        for i, f in enumerate(data["findings"]):
            head = "## {} {}. {}:{}".format(
                _SEV_ICON.get(f["severity"], "•"),
                i + 1,
                _interp(_alt(f["path"], "?")),
                _interp(_alt(f["line"], "?")),
            )
            if _truthy(f["quick_win"]):
                head += "  ⚡"
            em.append(head)
            parts = []
            if len(f["tags"]) > 0:
                parts.append(" · ".join(f["tags"]))
            if not _truthy(f["is_bot"]):
                parts.append("by @{}".format(_interp(f["root_author"])))
            if len(parts) > 0:
                em.append("_" + " · ".join(parts) + "_")
            em.append("")
            for k, c in enumerate(f["comments"]):
                if k == 0:
                    em.append(c["body"])
                else:
                    em.append(
                        "\n> **@{}:** ".format(_interp(c["author"]))
                        + c["body"].replace("\n", "\n> ")
                    )
            em.append("")
            em.append("↳ reply-to id: `{}`".format(_interp(f["reply_to"])))
            em.append("")
            em.append("---")
    # Resolved block: the shell passed --argjson all 0/1 and jq treats BOTH as
    # truthy (only false/null are falsy), so the block renders whenever
    # resolved findings exist, --all or not. Differential-tested byte-for-byte
    # against the shell, so replicate that behavior rather than the flag doc.
    if len(data["resolved_findings"]) > 0:
        em.append("")
        em.append(
            "<details><summary>{} resolved</summary>".format(len(data["resolved_findings"]))
        )
        for r in data["resolved_findings"]:
            em.append(
                "\n- {}:{} — {}".format(
                    _interp(_alt(r["path"], "?")),
                    _interp(_alt(r["line"], "?")),
                    _interp(r["title"]),
                )
            )
        em.append("</details>")
    # jq -r prints each emitted string followed by a newline.
    return "".join(e + "\n" for e in em)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _read_file(path: str) -> str:
    """cat + command substitution: file contents with trailing newlines
    stripped; a missing/unreadable file degrades to empty (set -e is off).

    The shell's `cat` was NOT redirected, so a mistyped seam path still said so
    on stderr; without that the only symptom is a bare "failed to parse review
    threads" and no hint of which file was unreadable. Diagnose on stderr only
    — stdout and the exit code stay exactly as the degraded read produced."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().rstrip("\n")
    except OSError as exc:
        sys.stderr.write("cannot read {}: {}\n".format(path, exc.strerror or exc))
        return ""


def main(argv: List[str]) -> int:
    force_utf8()
    pr = ""
    repo = os.environ.get("GH_REPO", "")
    want_json = want_all = no_summary = full = False
    for a in argv:
        if a == "--json":
            want_json = True
        elif a == "--all":
            want_all = True
        elif a == "--no-summary":
            no_summary = True
        elif a == "--full":
            full = True
        elif a.startswith("-"):
            sys.stderr.write("unknown flag: {}\n".format(a))
            return 2
        elif "/" in a:
            repo = a
        else:
            if not pr:
                pr = a
            else:
                repo = a

    if not pr:
        r = ["-R", repo] if repo else []
        res = run_command(["gh", "pr", "view", *r, "--json", "number", "-q", ".number"])
        pr = res.out.rstrip("\n")
    if not pr:
        print('{"error":"no PR (pass a PR number, and OWNER/REPO unless run inside the repo)"}')
        return 0

    if repo:
        i, j = repo.find("/"), repo.rfind("/")
        owner = repo[:i] if i >= 0 else repo
        name = repo[j + 1 :] if j >= 0 else repo
    else:
        res = run_command(
            ["gh", "repo", "view", "--json", "owner,name", "-q", '.owner.login + " " + .name']
        )
        ownername = res.out.rstrip("\n")
        i, j = ownername.find(" "), ownername.rfind(" ")
        owner = ownername[:i] if i >= 0 else ownername
        name = ownername[j + 1 :] if j >= 0 else ownername

    # --- Fetch review threads (or read a saved fixture) ---
    threads_file = os.environ.get("PRC_THREADS_FILE", "")
    if threads_file:
        threads_text = _read_file(threads_file)
    else:
        res = gh_retry(
            [
                "gh", "api", "graphql",
                "-F", "owner=" + owner,
                "-F", "repo=" + name,
                "-F", "pr=" + pr,
                "-f", "query=" + _THREADS_QUERY,
            ]
        )
        threads_text = res.out.rstrip("\n")
    if not threads_text:
        threads_text = "{}"

    # The seam may hold a whole --paginate stream, so decode every document.
    threads_docs = json_documents(threads_text)
    if not threads_docs:
        # Unparseable, or nothing but whitespace (a lone BOM included): both
        # landed on the "failed to parse" report below in the shell
        # (`jq -n --argjson threads '   '`) — never on a clean "0 unresolved".
        threads_docs = None

    # Be honest about a failed fetch: a GraphQL error or a null payload must
    # not be reported as "0 unresolved" — for a babysit tool that could
    # green-light a merge on a stale read. A PR with genuinely zero threads
    # still has a non-null payload. (Unparseable text falls through to the
    # "failed to parse" report below, like the shell's jq pipeline did.)
    # Every document in the stream must be healthy; see is_failed_fetch for why
    # that is deliberately stricter than the shell's last-document-wins gate.
    if threads_docs is not None and is_failed_fetch(threads_docs):
        print(
            '{"error":"failed to fetch review threads'
            ' (GraphQL error, auth, or transient API hiccup) — retry"}'
        )
        return 0

    # --- Fetch the reviewer-bot PR-level summary (one issue comment it keeps
    #     updated) ---
    summary_obj: Any = []
    if not no_summary:
        summary_file = os.environ.get("PRC_SUMMARY_FILE", "")
        if summary_file:
            stext = _read_file(summary_file) or "[]"
            sdocs = json_documents(stext)
            if not sdocs:
                summary_obj = _INVALID  # verdict jq failed to parse -> ""
            elif len(sdocs) == 1:
                # One document: hand it over as-is, object included — the
                # verdict program maps over an object's values (see
                # extract_verdict), so unwrapping it here would lose comments.
                summary_obj = sdocs[0]
            else:
                # A saved `gh api ... --paginate` stream: merge the pages
                # exactly like the live path a few lines below does. Sanctioned
                # deviation: the shell's seam fed the stream straight to jq,
                # which ran the verdict program once per document and printed a
                # mangled multi-line verdict; merging here makes the seam agree
                # with the shell's LIVE path, which is the behavior that counts.
                summary_obj = merge_paginated(stext)
        else:
            # Deliberately not gh_retry (the shell called gh bare here): the
            # summary is decorative, and a failed read degrades to [].
            res = run_command(
                ["gh", "api", "repos/{}/{}/issues/{}/comments".format(owner, name, pr), "--paginate"]
            )
            summary_obj = merge_paginated(res.out)

    try:
        # Accepted strictness: json.loads rejects what jq's --argjson scanner
        # accepted (a zero-padded "007" parsed there, and lands on the "failed
        # to parse" report here). Demanding a plain JSON number for a PR number
        # is the better contract, so keep it rather than re-implement jq.
        pr_val: Any = json.loads(pr)  # --argjson pr: a non-JSON pr fails the build
    except ValueError:
        pr_val = _INVALID
    data = None
    if pr_val is not _INVALID and threads_docs is not None:
        threads_obj = merge_threads(threads_docs)
        if threads_obj is not _INVALID:
            data = build_data(threads_obj, pr_val, "{}/{}".format(owner, name), full)
    if data is None:
        print('{"error":"failed to parse review threads"}')
        return 0

    verdict = extract_verdict(summary_obj)

    if want_json:
        data["actionable_verdict"] = verdict  # appended last, like jq's `. + {…}`
        # Accepted difference from jq's printer: jq escapes U+007F (DEL)
        # as a \u007f sequence where ensure_ascii=False writes it raw. Both
        # documents parse to the same value, so no consumer can tell them
        # apart — not worth a custom encoder.
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    sys.stdout.write(render_markdown(data, verdict, want_all))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
