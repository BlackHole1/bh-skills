"""Tests for ship-pr/scripts/pr_comments.py.

Two layers:

1. Unit tests over the pure cleaning/classification functions. These encode the
   regressions the shell version's comments call out: an unclosed fence must
   protect nothing, an inline `<!--` in prose is author text, a NOISE section
   drops its whole subtree, a summary label that merely *contains* a noise
   phrase survives (the pattern is anchored), and a human login that contains a
   bot keyword ("precursor") is not a bot.

2. End-to-end runs of the script over tests/fixtures/*.json through the
   PRC_THREADS_FILE / PRC_SUMMARY_FILE seams — no gh, no network. Those
   fixtures are the same ones used for the one-off shell-vs-Python differential
   (bash + jq), which is deliberately NOT part of this suite: the Windows CI
   runner has neither bash nor jq.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pr_comments
from gh_retry import RunResult
from pr_comments import (
    clean,
    extract_verdict,
    header_tags,
    is_bot,
    is_failed_fetch,
    is_tag_header,
    json_documents,
    merge_paginated,
    merge_threads,
    sev_first,
    sev_of,
    sev_rank,
    sev_text,
    strip_header_line,
    title_of,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fx(name):
    return str(FIXTURES / name)


def load(name):
    with open(fx(name), encoding="utf-8") as fh:
        return json.load(fh)


def seam(threads, summary=None):
    """Env that points both implementations at saved payloads."""
    env = {"PRC_THREADS_FILE": fx(threads)}
    if summary:
        env["PRC_SUMMARY_FILE"] = fx(summary)
    return env


def body(*lines):
    return "\n".join(lines)


def stream(tmp_path, name, *docs):
    """Write CONCATENATED JSON documents — what `gh api ... --paginate > f.json`
    saves — and return the path, for the PRC_* seams to read."""
    path = tmp_path / name
    path.write_text(
        "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in docs), encoding="utf-8"
    )
    return str(path)


def threads_page(*nodes):
    return {
        "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": list(nodes)}}}}
    }


def thread_node(path, resolved=False, login="coderabbitai", text="**Finding.**"):
    return {
        "isResolved": resolved,
        "isOutdated": False,
        "path": path,
        "line": 7,
        "startLine": None,
        "comments": {
            "nodes": [
                {
                    "databaseId": 11,
                    "author": {"login": login},
                    "createdAt": "2024-01-01T00:00:00Z",
                    "body": text,
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# clean() — HTML comments
# ---------------------------------------------------------------------------


def test_clean_strips_own_line_html_comments():
    src = body(
        "<!-- This is an auto-generated comment by CodeRabbit -->",
        "",
        "**Real finding.**",
        "",
        "<!-- fingerprinting:phantom:poseidon:whale -->",
    )
    assert clean(src, True) == "**Real finding.**"


def test_clean_eats_a_multiline_comment_with_a_base64_state_blob():
    src = body(
        "**Real finding.**",
        "",
        "<!--",
        '{"state":"H4sIAAAAAAAAA1WOwQqDMBBEf2XZcxHtwUJvpZfSQqG9lB4="}',
        "-->",
        "",
        "Tail prose.",
    )
    assert clean(src, True) == "**Real finding.**\n\nTail prose."


def test_clean_keeps_real_text_after_a_multiline_comment_closes():
    src = body("**Finding.**", "", "<!-- open", "still inside", "--> tail kept")
    # Only the comment itself is eaten; the text after "-->" survives with the
    # spacing it had (the final trim only touches the ends of the whole body).
    assert clean(src, True) == "**Finding.**\n\n tail kept"


def test_clean_unterminated_trailing_comment_opener_eats_to_end():
    # A truncated state blob must not leave half a base64 dump in the output.
    src = body("**Finding.**", "", "<!-- fingerprinting:phantom", "b64junk")
    assert clean(src, True) == "**Finding.**"


def test_clean_keeps_an_inline_comment_opener_in_prose():
    # Only an HTML comment that OWNS the line is bot chrome; inline is author
    # text and must survive verbatim, in bot and human bodies alike.
    src = body("See <!-- ref: #12 --> the discussion.", "<!-- own line -->", "End.")
    assert clean(src, False) == "See <!-- ref: #12 --> the discussion.\n\nEnd."
    assert clean(src, True) == "See <!-- ref: #12 --> the discussion.\n\nEnd."


# ---------------------------------------------------------------------------
# clean() — <details> handling
# ---------------------------------------------------------------------------


NOISE_BODY = body(
    "**Finding.**",
    "",
    "<details>",
    "<summary>📝 Committable suggestion</summary>",
    "",
    "dropped",
    "",
    "</details>",
    "",
    "<details>",
    "<summary>🤖 Prompt for AI Agents</summary>",
    "dropped",
    "</details>",
    "",
    "<details>",
    "<summary>🧰 Tools</summary>",
    "<details>",
    "<summary>🪛 Ruff (0.8.2)</summary>",
    "12-12: Unused import",
    "</details>",
    "</details>",
    "",
    "<details>",
    "<summary>✨ Finishing Touches</summary>",
    "dropped",
    "</details>",
    "",
    "<details>",
    "<summary>💡 Tips</summary>",
    "dropped",
    "</details>",
    "",
    "Also applies to: 30-34",
)


def test_clean_drops_every_noise_section_with_its_subtree():
    assert clean(NOISE_BODY, True) == "**Finding.**\n\nAlso applies to: 30-34"


def test_clean_leaves_noise_sections_alone_for_human_authors():
    # The noise-dropping is reviewer-bot chrome; a human quoting the same
    # markup keeps it.
    out = clean(NOISE_BODY, False)
    assert "<summary>📝 Committable suggestion</summary>" in out
    assert "12-12: Unused import" in out


def test_clean_unwraps_a_kept_summary_label_to_bold():
    src = body("<details>", "<summary>🔍 Analysis</summary>", "prose", "</details>")
    assert clean(src, True) == "**🔍 Analysis**\n\nprose"


def test_clean_summary_label_containing_a_noise_phrase_survives():
    # NOISE is anchored at the START of the label, so "How to simplify code"
    # is kept even though "simplify code" is a noise phrase.
    src = body(
        "<details>",
        "<summary>How to simplify code</summary>",
        "keep this advice",
        "</details>",
    )
    assert clean(src, True) == "**How to simplify code**\n\nkeep this advice"


def test_clean_drops_a_noise_section_nested_inside_a_kept_one():
    src = body(
        "<details>",
        "<summary>How to simplify code</summary>",
        "",
        "keep the intro",
        "",
        "<details>",
        "<summary>🪛 markdownlint-cli2 (0.17.2)</summary>",
        "42-42: Bare URL used",
        "</details>",
        "",
        "keep the outro",
        "",
        "</details>",
    )
    assert clean(src, True) == (
        "**How to simplify code**\n\nkeep the intro\n\nkeep the outro"
    )


def test_clean_handles_the_combined_one_line_details_summary_form():
    src = body(
        "<details><summary>📝 Committable suggestion</summary>",
        "dropped",
        "</details>",
        "kept",
    )
    assert clean(src, True) == "kept"


def test_clean_empty_summary_label_emits_nothing():
    assert clean(body("<summary></summary>", "kept"), True) == "kept"


def test_clean_collapses_blank_runs_and_trailing_whitespace():
    src = body("a   ", "", "", "", "b")
    assert clean(src, True) == "a\n\nb"


# ---------------------------------------------------------------------------
# clean() — fences
# ---------------------------------------------------------------------------


def test_clean_backtick_fence_protects_details_and_comment_markup():
    src = body(
        "Docs render raw markup:",
        "",
        "```html",
        "<details>",
        "<summary>📝 Committable suggestion</summary>",
        "<!-- not a real state blob -->",
        "</details>",
        "```",
        "",
        "end",
    )
    assert clean(src, True) == src


def test_clean_tilde_fence_closes_on_a_longer_marker():
    src = body("~~~markdown", "<summary>🧰 Tools</summary>", "<!-- keep -->", "~~~~")
    assert clean(src, True) == src


def test_clean_four_backtick_fence_holds_three_backtick_fences():
    src = body("````", "```", "<summary>💡 Tips</summary>", "```", "````")
    assert clean(src, True) == src


def test_clean_tilde_fence_is_not_closed_by_backticks():
    # Different fence chars never pair; the backtick line stays unprotected
    # content of an unclosed tilde fence, so the summary IS processed.
    src = body("~~~", "```", "<summary>🧰 Tools</summary>", "kept")
    assert clean(src, True) == "~~~\n```"  # summary is NOISE -> drops the tail


def test_clean_backtick_fence_info_string_with_a_backtick_is_not_an_opener():
    src = body(
        "``` `weird` info string",
        "<summary>Not protected</summary>",
        "after",
    )
    assert clean(src, True) == (
        "``` `weird` info string\n\n**Not protected**\n\nafter"
    )


def test_clean_unclosed_fence_protects_nothing():
    src = body("```text", "<!-- own-line comment, still stripped -->", "visible")
    assert clean(src, True) == "```text\n\nvisible"


def test_clean_fence_opener_indented_more_than_three_spaces_is_not_a_fence():
    src = body("intro", "    ```", "<!-- stripped -->", "    ```")
    assert clean(src, True) == "intro\n    ```\n\n    ```"


def test_clean_fence_closes_on_a_longer_marker_of_the_same_char():
    src = body("```", "<summary>🧰 Tools</summary>", "````", "kept after")
    assert clean(src, True) == src


# ---------------------------------------------------------------------------
# clean() — the sanctioned --full fix (the shell's flag was dead)
# ---------------------------------------------------------------------------


def test_clean_full_keeps_every_details_section_but_still_strips_comments():
    out = clean(NOISE_BODY, True, full=True)
    assert "<summary>📝 Committable suggestion</summary>" in out
    assert "<summary>🪛 Ruff (0.8.2)</summary>" in out
    assert "Also applies to: 30-34" in out
    assert "**📝 Committable suggestion**" not in out  # labels are NOT unwrapped


def test_clean_full_still_strips_own_line_comments_and_honors_fences():
    src = body(
        "<!-- chrome -->",
        "```html",
        "<!-- inside a fence -->",
        "```",
        "<!-- more chrome -->",
        "tail",
    )
    assert clean(src, True, full=True) == (
        "```html\n<!-- inside a fence -->\n```\n\ntail"
    )


# ---------------------------------------------------------------------------
# is_bot
# ---------------------------------------------------------------------------


def test_is_bot_recognizes_the_github_app_suffix():
    assert is_bot("greptile-apps[bot]")
    assert is_bot("sourcery-ai[bot]")
    assert is_bot("SOME-RANDOM-APP[BOT]")  # ascii_downcase first


@pytest.mark.parametrize(
    "slug",
    [
        "coderabbitai",
        "coderabbit",
        "sourcery",
        "sourceryai",
        "codium",
        "codiumai",
        "qodo",
        "greptile",
        "ellipsis",
        "bugbot",
        "cursor",
    ],
)
def test_is_bot_recognizes_each_bare_reviewer_slug(slug):
    assert is_bot(slug)
    assert is_bot(slug.upper())


@pytest.mark.parametrize(
    "login", ["cursorfan", "precursor", "coderabbitai-fan", "not-cursor", "octocat"]
)
def test_is_bot_is_anchored_so_humans_containing_a_keyword_are_not_bots(login):
    assert not is_bot(login)


def test_is_bot_tolerates_a_missing_login():
    assert not is_bot(None)
    assert not is_bot("")


# ---------------------------------------------------------------------------
# Tag header / severity / title
# ---------------------------------------------------------------------------


def test_is_tag_header_matches_the_coderabbit_header():
    assert is_tag_header("_⚠️ Potential issue_ | _🔴 Critical_")
    assert is_tag_header("  _🧹 Nitpick (assertive)_ | _⚡ Quick win_  ")


def test_is_tag_header_rejects_a_human_italic_line_with_an_emoji():
    # The strictness (no prose between the underscore tokens) is what keeps a
    # human's emphasis line from being eaten as bot chrome.
    assert not is_tag_header("_note_ and _ok_ 🔧")
    assert not is_tag_header("this _is_ prose 🔧")


def test_is_tag_header_requires_a_symbol():
    assert not is_tag_header("_just_ | _words_")


def test_header_tags_splits_the_first_nonblank_line():
    src = body("", "  ", "_⚠️ Potential issue_ | _🔴 Critical_", "", "body")
    assert header_tags(src) == ["⚠️ Potential issue", "🔴 Critical"]


def test_header_tags_returns_empty_when_there_is_no_header():
    assert header_tags("## 🔴 Logic error\n\nprose") == []
    assert header_tags("") == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("🔴 Critical", "critical"),
        ("Critical issue", "critical"),
        ("CRITICAL", "critical"),
        ("🟠 Major", "major"),
        ("major refactor", "major"),
        ("🟡 Minor", "minor"),
        ("🔵 Trivial", "minor"),
        ("🧹 Nitpick", "nitpick"),
        ("nitpick (assertive)", "nitpick"),
        ("📋 Documentation", "other"),
        ("", "other"),
    ],
)
def test_sev_text(text, expected):
    assert sev_text(text) == expected


def test_sev_of_joins_tags_and_takes_the_most_severe_match():
    # The checks run critical -> major -> minor -> nitpick, so 🟡 wins over 🧹.
    assert sev_of(["🧹 Nitpick", "🟡 Minor"]) == "minor"
    assert sev_of(["🧹 Nitpick (assertive)", "⚡ Quick win"]) == "nitpick"
    assert sev_of([]) == "other"


def test_sev_first_classifies_a_heading_style_body_by_emoji_only():
    assert sev_first("## 🔴 Logic error\n\nprose") == "critical"
    assert sev_first("\n\n## 🟠 Perf\n") == "major"
    assert sev_first("## 🟡 Style") == "minor"
    assert sev_first("## 🔵 Info") == "minor"
    assert sev_first("## 🧹 Tidy") == "nitpick"


def test_sev_first_ignores_severity_words_and_later_lines():
    # Words on the first line would be prose false positives; emoji further
    # down belong to a different finding.
    assert sev_first("## Critical section of the parser") == "other"
    assert sev_first("intro line\n## 🔴 Logic error") == "other"


def test_sev_rank_orders_the_known_severities_and_defaults_last():
    ranks = [sev_rank(s) for s in ("critical", "major", "minor", "nitpick", "other")]
    assert ranks == [0, 1, 2, 3, 4]
    assert sev_rank("unheard-of") == 5


def test_title_of_prefers_the_first_bold_line():
    src = body("_⚠️ Potential issue_ | _🔴 Critical_", "", "**Fix the guard.**", "more")
    assert title_of(src) == "Fix the guard."


def test_title_of_falls_back_to_the_first_line_and_strips_markup():
    assert title_of("## 🔴 Logic error\n\nprose") == "🔴 Logic error"
    assert title_of("<b>tagged</b> title") == "tagged title"
    assert title_of("") == ""


def test_strip_header_line_removes_only_a_leading_tag_header():
    src = body("_⚠️ Potential issue_ | _🔴 Critical_", "", "**Fix it.**")
    assert strip_header_line(src) == "**Fix it.**"
    plain = body("**Fix it.**", "", "_emphasis_ later")
    assert strip_header_line(plain) == plain
    assert strip_header_line("") == ""


# ---------------------------------------------------------------------------
# json_documents / merge_paginated / merge_threads / extract_verdict
# ---------------------------------------------------------------------------


def test_json_documents_decodes_a_concatenated_stream():
    # jq read a document *stream* wherever the shell piped raw text into it, so
    # a `gh api ... --paginate > f.json` save has to parse here too.
    assert json_documents('{"a":1}\n{"a":2}\n') == [{"a": 1}, {"a": 2}]
    assert json_documents("[1] [2]") == [[1], [2]]
    assert json_documents('{"a":1}') == [{"a": 1}]  # single document unchanged


def test_json_documents_separates_nothing_from_unparseable():
    # [] means "the text held no document" and None means "not JSON". No caller
    # acts on the difference today — main() collapses both onto the "failed to
    # parse review threads" report, and merge_paginated maps both to [] — but
    # the return value keeps them apart so a caller that wants to distinguish an
    # empty read from a broken one can, without re-parsing.
    assert json_documents("") == []
    assert json_documents("   \n ") == []
    assert json_documents("gh: not found") is None
    assert json_documents('[{"id":1}] oops') is None


BOM = chr(0xFEFF)  # spelled out: an invisible literal in the source helps nobody


def test_json_documents_skips_a_leading_utf8_bom():
    # jq skips a BOM; raw_decode would choke on it and downgrade a perfectly
    # good --paginate save into the "failed to parse review threads" report.
    assert json_documents(BOM + '{"a":1}\n{"a":2}\n') == [{"a": 1}, {"a": 2}]
    assert json_documents(BOM + '[{"id":1}]') == [[{"id": 1}]]
    assert merge_paginated(BOM + '[{"id":1}]\n[{"id":2}]\n') == [{"id": 1}, {"id": 2}]


def test_json_documents_treats_a_bom_only_input_as_nothing_read():
    # Only the stream's leading BOM is skipped, so what is left is whitespace at
    # most: the empty result every call site already treats as nothing read.
    assert json_documents(BOM) == []
    assert json_documents(BOM + "  \n ") == []
    assert merge_paginated(BOM) == []
    # A second BOM is not whitespace — that is text jq could not parse either.
    assert json_documents(BOM + BOM) is None


def test_merge_paginated_concatenates_the_arrays_gh_paginate_emits():
    text = '[{"id":1},{"id":2}]\n[{"id":3}]\n'
    assert merge_paginated(text) == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_merge_paginated_degrades_to_empty_on_nothing_or_garbage():
    assert merge_paginated("") == []
    assert merge_paginated("   \n ") == []
    assert merge_paginated("gh: not found") == []
    assert merge_paginated('[{"id":1}] oops') == []


def test_merge_paginated_treats_a_lone_object_as_an_unusable_page():
    # The port's own contract, not the shell's: `jq -s 'add // []'` over a
    # stream holding a single OBJECT returns that object, not []. The two are
    # boundary-equivalent at the only place it shows — the verdict program maps
    # over the object's values, hits a string where a comment belongs and errors
    # out — so the observable verdict is empty either way.
    assert merge_paginated('{"message":"Not Found"}') == []
    assert extract_verdict({"message": "Not Found"}) == ""


def test_merge_threads_returns_a_lone_document_untouched():
    doc = threads_page(thread_node("a.py"))
    assert merge_threads([doc]) is doc


def test_merge_threads_concatenates_the_nodes_of_every_page():
    merged = merge_threads(
        [threads_page(thread_node("a.py")), threads_page(thread_node("b.py"))]
    )
    nodes = merged["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    assert [n["path"] for n in nodes] == ["a.py", "b.py"]


def test_merge_threads_tolerates_a_page_that_carries_no_payload():
    # The fold is total: a payload-less page contributes no nodes instead of
    # blowing up. This is a lower-level guarantee only — such a stream never
    # reaches merge_threads through main(), because is_failed_fetch rejects the
    # whole read first (see the honesty-gate tests below).
    merged = merge_threads(
        [{"errors": [{"message": "boom"}], "data": None}, threads_page(thread_node("a.py"))]
    )
    nodes = merged["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    assert [n["path"] for n in nodes] == ["a.py"]


# ---------------------------------------------------------------------------
# is_failed_fetch — the honesty gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "docs",
    [
        [{"errors": [{"message": "boom"}], "data": None}],
        [{"data": None}],
        [{}],  # no payload key at all (the empty-read "{}" default)
        [None],
        # Unhealthy FIRST, healthy last: the shell's `jq -e` took its status
        # from the last document and let this through. We do not.
        [{"errors": [{"message": "boom"}], "data": None}, threads_page(thread_node("a.py"))],
        [{"errors": [{"message": "boom"}], "data": {"repository": None}}, threads_page()],
        # Unhealthy last.
        [threads_page(thread_node("a.py")), {"data": None}],
        # Unhealthy in the middle of healthy pages.
        [threads_page(), {"errors": [{"message": "boom"}], "data": None}, threads_page()],
    ],
)
def test_is_failed_fetch_fails_closed_on_any_unhealthy_document(docs):
    assert is_failed_fetch(docs) is True


@pytest.mark.parametrize(
    "docs",
    [
        [threads_page()],  # a real PR with genuinely zero threads
        [threads_page(thread_node("a.py"))],
        [threads_page(thread_node("a.py")), threads_page()],
        # The per-document predicate is the shell's, `.errors or (.data==null)`,
        # so a null `errors` key is not an error and a non-null payload passes.
        [{"data": {"repository": None}, "errors": None}],
        # Not objects at all: not judged here — merge_threads/build_data report
        # them as "failed to parse", a different (and equally honest) message.
        ["not a payload"],
        [[]],
    ],
)
def test_is_failed_fetch_passes_a_healthy_or_unjudgeable_stream(docs):
    assert is_failed_fetch(docs) is False


def test_merge_threads_rejects_a_stream_holding_a_non_payload():
    assert merge_threads(["nope", threads_page()]) is pr_comments._INVALID
    assert merge_threads([threads_page(), {"data": {"repository": 7}}]) is (
        pr_comments._INVALID
    )


def test_extract_verdict_takes_the_newest_bot_comment():
    # gh returns issue comments oldest-first; the shell reversed the bot bodies
    # so the LAST one (the bot keeps editing/reposting) decides.
    assert extract_verdict(load("summary_actionable.json")) == (
        "Actionable comments posted: 3"
    )


def test_extract_verdict_falls_back_to_the_no_actionable_sentence():
    assert extract_verdict(load("summary_none.json")) == (
        "No actionable comments were generated"
    )


def test_extract_verdict_ignores_a_human_who_typed_the_magic_sentence():
    # summary_none.json's second comment is a human writing "Actionable
    # comments posted: 99" — it must not become the verdict.
    assert "99" not in extract_verdict(load("summary_none.json"))


def test_extract_verdict_is_empty_when_neither_pattern_is_present():
    assert extract_verdict(load("summary_neither.json")) == ""
    assert extract_verdict([]) == ""


def test_extract_verdict_degrades_on_a_malformed_payload():
    assert extract_verdict(None) == ""
    assert extract_verdict("nonsense") == ""
    assert extract_verdict([{"user": {"login": "coderabbitai"}, "body": None}]) == ""


def test_extract_verdict_matches_case_insensitively():
    payload = [{"user": {"login": "qodo-merge-pro[bot]"},
                "body": "ACTIONABLE COMMENTS POSTED:  4"}]
    assert extract_verdict(payload) == "Actionable comments posted: 4"


# ---------------------------------------------------------------------------
# End to end over the fixtures (PRC seams; no gh, no network)
# ---------------------------------------------------------------------------


def test_markdown_renders_the_full_coderabbit_fixture(run_script):
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env=seam("threads_coderabbit.json", "summary_actionable.json"),
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.startswith("# Review comments — PR #123  (acme/widgets)\n")
    assert "8 unresolved · 2 resolved · Actionable comments posted: 3\n" in out
    assert "severity: critical 2 · major 1 · minor 1 · nitpick 1 · other 3\n" in out
    # Findings are numbered in severity-rank order.
    assert "## 🔴 1. src/app.py:12\n" in out
    assert "## 🔴 2. src/auth.py:77\n" in out
    assert "## 🟠 3. src/retry.py:88\n" in out
    assert "## 🟡 4. src/paths.py:5\n" in out
    assert "## 🧹 5. src/util.py:41  ⚡\n" in out  # quick-win marker
    assert "## • 6. README.md:3\n" in out
    # line is null on that thread; startLine (42) is the fallback.
    assert "## • 8. src/ghost.py:42\n" in out
    assert "↳ reply-to id: `2101`\n" in out


def test_markdown_bot_chrome_is_gone_but_the_finding_survives(run_script):
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env=seam("threads_coderabbit.json", "summary_actionable.json"),
    )
    out = r.stdout
    assert "**Guard the index before dereferencing `items[idx]`.**" in out
    assert "Also applies to: 30-34" in out
    for noise in (
        "Committable suggestion",
        "Prompt for AI Agents",
        "🧰 Tools",
        "🪛 Ruff",
        "Finishing Touches",
        "💡 Tips",
        "H4sIAAAAAAAAA1",       # the base64 state blob
        "fingerprinting:phantom",
        "auto-generated comment",
    ):
        assert noise not in out, noise
    # A label that merely contains a noise phrase is kept, unwrapped to bold.
    assert "**How to simplify code**" in out
    assert "That keeps the diff small." in out
    assert "42-42: Bare URL used" not in out  # nested noise dropped with its subtree


def test_markdown_human_thread_keeps_prose_markup_and_bylines(run_script):
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env=seam("threads_coderabbit.json", "summary_actionable.json"),
    )
    out = r.stdout
    # "precursor" contains "cursor" but is a human: byline rendered, markup kept.
    assert "_by @precursor_" in out
    assert "<summary>My own collapsible</summary>" in out
    assert "This is fine, but see <!-- ref: #12 --> the discussion in #12." in out
    assert "<!-- reviewer scratch -->" not in out  # own-line chrome still goes
    # A comment whose author is null renders as "?".
    assert "_by @?_" in out


def test_markdown_blockquotes_the_replies_after_the_root_comment(run_script):
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env=seam("threads_coderabbit.json", "summary_actionable.json"),
    )
    assert (
        "> **@octocat:** Disagree — `os.path` is deliberate, we still support"
        " 3.5 here.\n" in r.stdout
    )
    assert (
        "> Second line mentions <!-- an inline opener --> and must stay whole.\n"
        in r.stdout
    )


def test_markdown_preserves_fenced_content_verbatim(run_script):
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env=seam("threads_fences.json", "summary_none.json"),
    )
    out = r.stdout
    for kept in (
        "```html\n<details>\n<summary>📝 Committable suggestion</summary>\n"
        "<!-- not a real state blob -->\n</details>\n```",
        "~~~markdown\n<summary>🧰 Tools</summary>\n<!-- keep me verbatim -->\n~~~~",
        "````\n```\n<summary>💡 Tips</summary>\n```\n````",
        "```sh\n<!-- this comment lives inside a fence and must survive -->\n"
        "echo hi\n```",
    ):
        assert kept in out, kept
    # Not an opener (backtick in the info string) -> the summary after it is
    # processed normally; the unclosed fence protects nothing.
    assert "``` `weird` info string\n" in out
    assert "**Not protected, so this gets unwrapped**" in out
    assert "```text\n" in out
    assert "own-line comment, still stripped" not in out
    assert "this line is emitted verbatim all the same" in out


def test_cleaner_corner_cases_render_as_the_shell_did(run_script):
    # One thread per corner case; these bodies are also part of the one-off
    # shell-vs-Python differential, so the strings below are shell-verified.
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets", "--json",
        env=seam("threads_clean_edges.json", "summary_neither.json"),
    )
    assert r.returncode == 0, r.stderr
    bodies = {
        f["path"]: f["comments"][0]["body"] for f in json.loads(r.stdout)["findings"]
    }
    assert bodies["tail.py"] == "**Finding.**\n\n trailing text kept"
    # A tilde fence is never closed by backticks, so nothing here is protected
    # and the unprotected NOISE summary swallows the rest of the body.
    assert bodies["tilde.py"] == "~~~\n```"
    assert bodies["indent.py"] == "intro\n    ```\n\n    ```"
    assert bodies["blanks.py"] == "a\n\nb"
    assert bodies["longclose.py"] == (
        "```\n<summary>🧰 Tools</summary>\n````\nkept after the longer closing marker"
    )


def test_json_payload_shape_and_ordering(run_script):
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets", "--json",
        env=seam("threads_coderabbit.json", "summary_actionable.json"),
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert list(data) == [
        "pr", "repo", "threads_total", "unresolved", "resolved", "by_severity",
        "findings", "resolved_findings", "actionable_verdict",
    ]
    assert (data["pr"], data["repo"]) == (123, "acme/widgets")
    assert (data["threads_total"], data["unresolved"], data["resolved"]) == (10, 8, 2)
    # by_severity keeps the canonical order and omits zero counts.
    assert list(data["by_severity"]) == ["critical", "major", "minor", "nitpick", "other"]
    assert data["by_severity"] == {
        "critical": 2, "major": 1, "minor": 1, "nitpick": 1, "other": 3
    }
    # findings are sorted by severity rank, stably within a rank.
    assert [f["severity"] for f in data["findings"]] == [
        "critical", "critical", "major", "minor", "nitpick", "other", "other", "other"
    ]
    assert [f["path"] for f in data["findings"]][:3] == [
        "src/app.py", "src/auth.py", "src/retry.py"
    ]
    assert data["actionable_verdict"] == "Actionable comments posted: 3"

    first = data["findings"][0]
    assert list(first) == [
        "resolved", "outdated", "path", "line", "reply_to", "root_author",
        "is_bot", "tags", "severity", "quick_win", "title", "comments",
    ]
    assert first["tags"] == ["⚠️ Potential issue", "🔴 Critical"]
    assert first["is_bot"] is True
    assert first["title"] == "Guard the index before dereferencing `items[idx]`."
    assert first["comments"][0]["body"].startswith("**Guard the index")

    quick = [f for f in data["findings"] if f["quick_win"]]
    assert [f["path"] for f in quick] == ["src/util.py"]
    assert quick[0]["outdated"] is True
    ghost = [f for f in data["findings"] if f["path"] == "src/ghost.py"][0]
    assert ghost["line"] == 42 and ghost["root_author"] == "?"
    assert [r_["path"] for r_ in data["resolved_findings"]] == ["src/io.py", None]


def test_resolved_block_lists_the_resolved_threads(run_script):
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets", "--all",
        env=seam("threads_coderabbit.json", "summary_actionable.json"),
    )
    assert r.returncode == 0, r.stderr
    assert "<details><summary>2 resolved</summary>" in r.stdout
    assert "- src/io.py:64 — Use a context manager for the file handle." in r.stdout
    assert "- ?:? — Nice catch, fixed in 9f3a1c2." in r.stdout  # null path/line
    assert r.stdout.rstrip("\n").endswith("</details>")


def test_resolved_block_also_renders_without_all(run_script):
    # Quirk faithfully carried over from the shell: --all was passed to jq as
    # --argjson all 0/1, and jq treats 0 as TRUTHY, so the block always
    # rendered. Differential-tested byte-for-byte; do not "fix" it silently.
    plain = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env=seam("threads_coderabbit.json", "summary_actionable.json"),
    )
    withall = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets", "--all",
        env=seam("threads_coderabbit.json", "summary_actionable.json"),
    )
    assert plain.stdout == withall.stdout


def test_no_summary_skips_the_verdict(run_script):
    env = seam("threads_coderabbit.json", "summary_actionable.json")
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets", "--no-summary", env=env
    )
    assert r.returncode == 0, r.stderr
    assert "8 unresolved · 2 resolved\n" in r.stdout
    assert "Actionable comments posted" not in r.stdout


def test_no_summary_leaves_the_json_verdict_empty(run_script):
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets", "--json", "--no-summary",
        env=seam("threads_coderabbit.json", "summary_actionable.json"),
    )
    assert json.loads(r.stdout)["actionable_verdict"] == ""


def test_full_keeps_the_details_sections(run_script):
    # Sanctioned fix: the shell parsed --full into jq as $full and then never
    # referenced it, so the flag was dead. Here it does what its doc says.
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets", "--full",
        env=seam("threads_coderabbit.json", "summary_actionable.json"),
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "<summary>📝 Committable suggestion</summary>" in out
    assert "<summary>🪛 Ruff (0.8.2)</summary>" in out
    assert "<summary>💡 Tips</summary>" in out
    assert "<summary>How to simplify code</summary>" in out  # NOT unwrapped
    assert "**How to simplify code**" not in out
    # Still strips own-line HTML comments, still honors fences.
    assert "H4sIAAAAAAAAA1" not in out
    assert "auto-generated comment" not in out
    assert "```suggestion\n    if idx < len(items):" in out


def test_full_still_honors_fences(run_script):
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets", "--full",
        env=seam("threads_fences.json", "summary_none.json"),
    )
    assert "```sh\n<!-- this comment lives inside a fence and must survive -->" in r.stdout
    assert "<!-- this one is own-line chrome and is stripped -->" not in r.stdout


def test_empty_thread_list_reports_no_unresolved_threads(run_script):
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env=seam("threads_empty.json", "summary_neither.json"),
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == (
        "# Review comments — PR #123  (acme/widgets)\n"
        "\n"
        "0 unresolved · 0 resolved\n"
        "\n"
        "_No unresolved review threads._\n"
    )


def test_degenerate_thread_shapes_do_not_crash(run_script):
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets", "--json",
        env=seam("threads_edge.json", "summary_none.json"),
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["unresolved"] == 4
    by_path = {f["path"]: f for f in data["findings"]}
    # No comments at all -> empty body, null reply-to, "?" author.
    assert by_path["a.py"]["reply_to"] is None
    assert by_path["a.py"]["comments"] == []
    assert by_path["b.py"]["comments"] == []  # comments key missing entirely
    assert by_path["c.py"]["comments"][0]["body"] == ""  # body was all chrome
    assert by_path["d.py"]["comments"][0]["body"] == "survives the stray close tag"


ERROR_FETCH = (
    '{"error":"failed to fetch review threads (GraphQL error, auth, or'
    ' transient API hiccup) — retry"}\n'
)


@pytest.mark.parametrize("fixture", ["threads_errors.json", "threads_null_data.json"])
def test_honesty_gate_reports_a_failed_fetch_and_exits_zero(run_script, fixture):
    # A GraphQL error or a null payload must never be reported as
    # "0 unresolved" — that could green-light a merge on a stale read.
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env=seam(fixture, "summary_actionable.json"),
    )
    assert r.returncode == 0
    assert r.stdout == ERROR_FETCH


def test_honesty_gate_also_fires_on_an_empty_payload(run_script, tmp_path):
    blank = tmp_path / "blank.json"
    blank.write_text("", encoding="utf-8")
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env={"PRC_THREADS_FILE": str(blank)},
    )
    assert r.returncode == 0
    assert r.stdout == ERROR_FETCH


def test_unparseable_payload_reports_a_parse_failure(run_script, tmp_path):
    junk = tmp_path / "junk.json"
    junk.write_text("gh: command not found", encoding="utf-8")
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env={"PRC_THREADS_FILE": str(junk)},
    )
    assert r.returncode == 0
    assert r.stdout == '{"error":"failed to parse review threads"}\n'


# ---------------------------------------------------------------------------
# Seam files holding a CONCATENATED stream (a `gh api ... --paginate` save)
# ---------------------------------------------------------------------------


def test_threads_seam_reads_a_concatenated_stream(run_script, tmp_path):
    # The shell piped the seam file's raw text into jq, which reads a document
    # stream; a --paginate save is exactly that and must not be rejected. No
    # page's threads may be dropped either — under-reporting unresolved threads
    # is the stale read the honesty gate exists to prevent.
    path = stream(
        tmp_path, "threads.json",
        threads_page(thread_node("a.py")),
        threads_page(thread_node("b.py"), thread_node("c.py", resolved=True)),
    )
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets", "--json",
        env={"PRC_THREADS_FILE": path},
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert (data["threads_total"], data["unresolved"], data["resolved"]) == (3, 2, 1)
    assert [f["path"] for f in data["findings"]] == ["a.py", "b.py"]


def test_threads_seam_reads_a_stream_behind_a_utf8_bom(run_script, tmp_path):
    # An editor (or a Windows redirect) can put a BOM in front of a saved
    # --paginate stream; that must not downgrade a good read into a report.
    path = tmp_path / "threads-bom.json"
    path.write_text(
        BOM
        + json.dumps(threads_page(thread_node("a.py")))
        + "\n"
        + json.dumps(threads_page(thread_node("b.py")))
        + "\n",
        encoding="utf-8",
    )
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets", "--json",
        env={"PRC_THREADS_FILE": str(path)},
    )
    assert r.returncode == 0, r.stderr
    assert [f["path"] for f in json.loads(r.stdout)["findings"]] == ["a.py", "b.py"]


def test_a_bom_only_threads_file_is_reported_not_rendered_as_zero(run_script, tmp_path):
    # BOM and nothing else is "nothing read". Like every other empty read it
    # must surface as a report, never as a clean "0 unresolved".
    path = tmp_path / "bom-only.json"
    path.write_text(BOM, encoding="utf-8")
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env={"PRC_THREADS_FILE": str(path)},
    )
    assert r.returncode == 0
    assert r.stdout == '{"error":"failed to parse review threads"}\n'


@pytest.mark.parametrize(
    "docs",
    [
        # The regression this gate was reopened to close: an error page whose
        # `data` is null, followed by a HEALTHY page that happens to hold zero
        # nodes. _dig walks the null payload to None, _alt turns the failed page
        # into zero threads, and a last-document-wins gate then green-lights the
        # read — rendering "0 unresolved" for a fetch that half failed.
        (
            {"errors": [{"message": "Something went wrong"}], "data": None},
            threads_page(),
        ),
        # Same shape, but the healthy page carries findings: still a partial
        # read, so still a report rather than an under-count.
        (
            {"errors": [{"message": "Something went wrong"}], "data": {"repository": None}},
            threads_page(thread_node("a.py")),
        ),
        # Unhealthy last, and unhealthy in the middle.
        (threads_page(thread_node("a.py")), {"data": None}),
        (
            threads_page(thread_node("a.py")),
            {"errors": [{"message": "boom"}], "data": None},
            threads_page(thread_node("b.py")),
        ),
    ],
)
def test_threads_seam_fails_closed_when_any_document_is_unhealthy(
    run_script, tmp_path, docs
):
    # Deliberately stricter than the shell: `jq -e` ran the gate once per
    # document and took its exit status from the LAST one, so a stream that
    # ended healthy passed. Fail-closed beats matching that accident — a
    # babysitting agent merges on "0 unresolved".
    path = stream(tmp_path, "threads.json", *docs)
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env={"PRC_THREADS_FILE": path},
    )
    assert r.returncode == 0
    assert r.stdout == ERROR_FETCH


def test_threads_seam_merges_a_stream_only_when_every_page_is_healthy(
    run_script, tmp_path
):
    # The other side of the gate: an all-healthy stream still merges, so the
    # strictness costs nothing on the paginated read it was built for.
    path = stream(
        tmp_path, "threads.json",
        threads_page(thread_node("a.py")),
        threads_page(),
        threads_page(thread_node("b.py")),
    )
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets", "--json",
        env={"PRC_THREADS_FILE": path},
    )
    assert r.returncode == 0, r.stderr
    assert [f["path"] for f in json.loads(r.stdout)["findings"]] == ["a.py", "b.py"]


def test_threads_seam_stream_with_a_non_payload_document_fails_the_parse(
    run_script, tmp_path
):
    path = stream(tmp_path, "threads.json", "not a payload", threads_page())
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env={"PRC_THREADS_FILE": path},
    )
    assert r.returncode == 0
    assert r.stdout == '{"error":"failed to parse review threads"}\n'


def test_summary_seam_reads_a_concatenated_stream(run_script, tmp_path):
    # The one that bites in practice: `gh api ... --paginate > f.json` is the
    # obvious way to save a summary, and it writes one array per page.
    path = stream(
        tmp_path, "summary.json",
        [{"user": {"login": "octocat"}, "body": "Looks good."}],
        [{"user": {"login": "coderabbitai[bot]"},
          "body": "Actionable comments posted: 5"}],
    )
    env = seam("threads_empty.json")
    env["PRC_SUMMARY_FILE"] = path
    r = run_script("ship-pr", "pr_comments.py", "123", "acme/widgets", "--json", env=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["actionable_verdict"] == "Actionable comments posted: 5"


def test_summary_seam_stream_keeps_a_verdict_from_an_earlier_page(run_script, tmp_path):
    path = stream(
        tmp_path, "summary.json",
        [{"user": {"login": "coderabbitai"},
          "body": "No actionable comments were generated"}],
        [{"user": {"login": "precursor"}, "body": "thanks"}],
    )
    env = seam("threads_empty.json")
    env["PRC_SUMMARY_FILE"] = path
    r = run_script("ship-pr", "pr_comments.py", "123", "acme/widgets", "--json", env=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["actionable_verdict"] == (
        "No actionable comments were generated"
    )


def test_summary_seam_stream_of_non_pages_degrades_to_no_verdict(run_script, tmp_path):
    # Two objects are not pages of comments; the merge degrades to [] exactly
    # like the live path's `jq -s 'add // []'` did.
    path = stream(tmp_path, "summary.json", {"message": "Not Found"}, {"message": "x"})
    env = seam("threads_empty.json")
    env["PRC_SUMMARY_FILE"] = path
    r = run_script("ship-pr", "pr_comments.py", "123", "acme/widgets", "--json", env=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["actionable_verdict"] == ""


def test_a_single_document_summary_object_still_yields_its_verdict(run_script, tmp_path):
    # Boundary with the stream handling above: one OBJECT is handed to the
    # verdict extractor untouched, because jq's map() iterates an object's
    # values. Merging it away would lose the comment.
    path = stream(
        tmp_path, "summary.json",
        {"c1": {"user": {"login": "coderabbitai[bot]"},
                "body": "Actionable comments posted: 2"}},
    )
    env = seam("threads_empty.json")
    env["PRC_SUMMARY_FILE"] = path
    r = run_script("ship-pr", "pr_comments.py", "123", "acme/widgets", "--json", env=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["actionable_verdict"] == "Actionable comments posted: 2"


# ---------------------------------------------------------------------------
# Unreadable seam paths (the shell's `cat` diagnostic)
# ---------------------------------------------------------------------------


def test_an_unreadable_threads_path_is_named_on_stderr(run_script, tmp_path):
    # The shell's `cat` was unredirected, so a mistyped path said so. Without it
    # the only symptom is the generic report below, with no hint of the file.
    missing = str(tmp_path / "typo-threads.json")
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env={"PRC_THREADS_FILE": missing},
    )
    assert r.returncode == 0
    assert r.stdout == ERROR_FETCH  # stdout and the exit code are untouched
    assert missing in r.stderr
    assert r.stderr.count("\n") == 1  # one tidy line, not a traceback


def test_an_unreadable_summary_path_is_named_but_still_degrades(run_script, tmp_path):
    missing = str(tmp_path / "typo-summary.json")
    env = seam("threads_empty.json")
    env["PRC_SUMMARY_FILE"] = missing
    r = run_script("ship-pr", "pr_comments.py", "123", "acme/widgets", "--json", env=env)
    assert r.returncode == 0
    assert json.loads(r.stdout)["actionable_verdict"] == ""
    assert missing in r.stderr
    assert r.stderr.count("\n") == 1


def test_a_readable_seam_file_says_nothing_on_stderr(run_script):
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets",
        env=seam("threads_empty.json", "summary_neither.json"),
    )
    assert r.returncode == 0
    assert r.stderr == ""


def test_unknown_flag_exits_two(run_script):
    r = run_script(
        "ship-pr", "pr_comments.py", "123", "acme/widgets", "--nope",
        env=seam("threads_empty.json"),
    )
    assert r.returncode == 2
    assert r.stderr == "unknown flag: --nope\n"
    assert r.stdout == ""


def test_a_malformed_summary_file_degrades_to_no_verdict(run_script, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all", encoding="utf-8")
    env = seam("threads_empty.json")
    env["PRC_SUMMARY_FILE"] = str(bad)
    r = run_script("ship-pr", "pr_comments.py", "123", "acme/widgets", "--json", env=env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["actionable_verdict"] == ""


# ---------------------------------------------------------------------------
# main() seams (no gh subprocess at all)
# ---------------------------------------------------------------------------


def test_no_pr_found_reports_it_and_exits_zero(monkeypatch, capsys):
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.delenv("PRC_THREADS_FILE", raising=False)
    monkeypatch.setattr(
        pr_comments, "run_command", lambda cmd, **kw: RunResult("", "", 1)
    )
    assert pr_comments.main([]) == 0
    assert capsys.readouterr().out == (
        '{"error":"no PR (pass a PR number, and OWNER/REPO unless run inside'
        ' the repo)"}\n'
    )


def test_the_threads_seam_short_circuits_gh_entirely(monkeypatch, capsys):
    def explode(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("gh must not be invoked when PRC_THREADS_FILE is set")

    monkeypatch.setattr(pr_comments, "run_command", explode)
    monkeypatch.setattr(pr_comments, "gh_retry", explode)
    monkeypatch.setenv("PRC_THREADS_FILE", fx("threads_empty.json"))
    monkeypatch.setenv("PRC_SUMMARY_FILE", fx("summary_neither.json"))
    assert pr_comments.main(["123", "acme/widgets"]) == 0
    assert "0 unresolved · 0 resolved" in capsys.readouterr().out


def test_gh_repo_env_supplies_the_default_repo(monkeypatch, capsys):
    monkeypatch.setenv("GH_REPO", "acme/widgets")
    monkeypatch.setenv("PRC_THREADS_FILE", fx("threads_empty.json"))
    monkeypatch.setenv("PRC_SUMMARY_FILE", fx("summary_neither.json"))
    assert pr_comments.main(["123"]) == 0
    assert "(acme/widgets)" in capsys.readouterr().out
