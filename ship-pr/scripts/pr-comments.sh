#!/usr/bin/env bash
# Fetch a PR's review threads (and the reviewer-bot summary), strip the noise that
# bots like CodeRabbit bury their findings in, and print clean Markdown — so the
# agent reads triage-ready content instead of spending tokens parsing raw GraphQL
# JSON full of HTML comments, base64 state blobs, and duplicated <details> blocks.
#
# What it removes from each *bot* comment body:
#   - every HTML comment <!-- ... --> (fingerprints, cr-comment ids, the giant
#     base64 "internal state" blob), even an unterminated/truncated trailing one
#   - the duplicated / verbose collapsible sections (📝 Committable suggestion,
#     🤖 Prompt for AI Agents, 🧰 Tools / 🪛 linter dumps, Finishing Touches, tips),
#     including everything nested inside them
# What it keeps: the finding prose, the path:line, a single suggested-fix diff,
# "Also applies to" lines, and the reply-to id you dispute false positives against.
# Human comments keep their full text (only the bot's own-line HTML-comment chrome
# is stripped; an inline `<!--` in prose or a code span is left verbatim) — the
# noise-dropping and <details> rewriting are reviewer-bot chrome, applied only to
# bot authors. Content inside fenced ``` / ~~~ code blocks is preserved verbatim
# (a reviewer quoting HTML or a diff is never mangled), using CommonMark-style
# fence matching (closing fence same char, length >= opener; unclosed fences
# protect nothing). What it computes for you (so the agent doesn't): per-finding
# severity / type / quick-win tags and a header tally (unresolved, resolved,
# by-severity counts, and the "Actionable comments posted: N" verdict).
#
# Usage: pr-comments.sh [PR_NUMBER] [OWNER/REPO] [flags]
#   --json         emit structured JSON instead of Markdown
#   --all          include resolved threads (default: unresolved only, + a count)
#   --no-summary   skip the reviewer-bot PR-level summary block
#   --full         keep every <details> section (only strips HTML comments)
#
#   PR_NUMBER  defaults to the current branch's PR (must run inside the repo)
#   OWNER/REPO defaults to $GH_REPO, else the repo of the current directory
#
# Testing seam: set PRC_THREADS_FILE / PRC_SUMMARY_FILE to read saved JSON
# instead of calling gh (lets the cleaning be unit-tested offline).
#
# Requires: gh (authenticated), jq.
set -uo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
. "$dir/gh-retry.sh"   # gh_retry: retry transient GitHub API EOF/5xx

pr=""; repo="${GH_REPO:-}"; want_json=0; want_all=0; no_summary=0; full=false
for a in "$@"; do
  case "$a" in
    --json) want_json=1 ;;
    --all) want_all=1 ;;
    --no-summary) no_summary=1 ;;
    --full) full=true ;;
    -*) echo "unknown flag: $a" >&2; exit 2 ;;
    */*) repo="$a" ;;
    *) [ -z "$pr" ] && pr="$a" || repo="$a" ;;
  esac
done

R=()
[ -n "$repo" ] && R=(-R "$repo")
if [ -z "$pr" ]; then
  pr="$(gh pr view "${R[@]+"${R[@]}"}" --json number -q .number 2>/dev/null)" || true
fi
if [ -z "$pr" ]; then
  echo '{"error":"no PR (pass a PR number, and OWNER/REPO unless run inside the repo)"}'
  exit 0
fi

if [ -n "$repo" ]; then
  owner="${repo%%/*}"; name="${repo##*/}"
else
  ownername="$(gh repo view --json owner,name -q '.owner.login + " " + .name' 2>/dev/null)" || true
  owner="${ownername%% *}"; name="${ownername##* }"
fi

# --- Fetch review threads (or read a saved fixture) ---
if [ -n "${PRC_THREADS_FILE:-}" ]; then
  threads="$(cat "$PRC_THREADS_FILE")"
else
  threads="$(gh_retry gh api graphql -F owner="$owner" -F repo="$name" -F pr="$pr" -f query='
  query($owner:String!,$repo:String!,$pr:Int!){
    repository(owner:$owner,name:$repo){
      pullRequest(number:$pr){
        reviewThreads(first:100){nodes{
          isResolved isOutdated path line startLine
          comments(first:50){nodes{ databaseId author{login} createdAt body }}
        }}
      }
    }
  }')" || true
fi
[ -z "$threads" ] && threads='{}'

# Be honest about a failed fetch: a GraphQL error or a null payload must not be
# reported as "0 unresolved" — for a babysit tool that could green-light a merge
# on a stale read. A PR with genuinely zero threads still has a non-null payload.
if printf '%s' "$threads" | jq -e '.errors or (.data == null)' >/dev/null 2>&1; then
  echo '{"error":"failed to fetch review threads (GraphQL error, auth, or transient API hiccup) — retry"}'
  exit 0
fi

# --- Fetch the reviewer-bot PR-level summary (one issue comment it keeps updated) ---
summary=""
if [ "$no_summary" -eq 0 ]; then
  if [ -n "${PRC_SUMMARY_FILE:-}" ]; then
    summary="$(cat "$PRC_SUMMARY_FILE")"
  else
    summary="$(gh api "repos/$owner/$name/issues/$pr/comments" --paginate 2>/dev/null \
      | jq -s 'add // []' 2>/dev/null)" || true
  fi
fi
[ -z "$summary" ] && summary='[]'

# ---------------------------------------------------------------------------
# The cleaner + renderer. All text processing happens here in jq so the agent
# never sees the raw noise. Long, but it runs in the script, not in context.
# ---------------------------------------------------------------------------
jq_prog='
# Collapsible sections of a *bot* body we drop wholesale (with everything nested
# inside them): pure duplication or verbose dumps. Applied only to bot authors.
# Matched against the lowercased <summary> label and anchored at the *start* of
# the label (CodeRabbit leads each with its marker), so a legit section whose
# title merely contains a phrase like "how to simplify code" is never dropped.
def NOISE:
  "^\\s*(🪛|🧰)"                                            # 🧰 Tools / 🪛 <linter> dumps (any sub-name)
  + "|^\\s*(📝|🤖|🧩|🧠|🚥|✨)?\\s*(committable suggestion|prompt for ai agents|tools|finishing touch(es)?|simplify code|pre-merge|analysis chain|outside diff range)\\b"
  + "|^\\s*(💡\\s*)?tips\\s*$";

# A login is a reviewer bot iff it is GitHub-App-suffixed (slug[bot]) or exactly
# one of the known reviewer slugs (CodeRabbit posts review comments as the bare
# "coderabbitai"). Anchored so a human login that merely *contains* a keyword
# (e.g. "cursorfan", "precursor") is never misread as a bot.
def is_bot($login):
  (($login // "") | ascii_downcase) as $l
  | ($l | test("\\[bot\\]$"))
    or ($l | test("^(coderabbitai|coderabbit|sourcery|sourceryai|codium|codiumai|qodo|greptile|ellipsis|bugbot|cursor)$"));

# Strip down a body to triage-ready Markdown via a two-pass, fence- and
# nesting-aware state machine (no flat sentinels, so unclosed fences and orphan
# tags can never swallow real signal).
#   $is_bot : when true, drop NOISE <details> subtrees and unwrap other
#             <summary> labels to **bold**; when false (human), leave all
#             <details>/<summary> markup untouched. Own-line HTML comments are
#             stripped for everyone; inline "<!--" (prose or code span) is kept.
def clean($is_bot):
  (split("\n")) as $lines
  | ($lines | length) as $n
  # --- Pass 1: matched fence pairs (CommonMark-ish: opener of N backticks/tildes
  #     closes only on a bare marker of the same char and length >= N; an
  #     unclosed opener protects nothing). $prot[i] => line i is inside a fence.
  | (reduce range(0; $n) as $i ({open:null, prot:{}};
       ($lines[$i] | [capture("^ {0,3}(?<f>`{3,}|~{3,})(?<rest>.*)$")] | .[0]) as $m
       | if $m == null then .
         elif (.open == null) then
           # opener — but a backtick fence info string may not contain backticks
           (if (($m.f[0:1]) == "`" and ($m.rest | test("`"))) then .
            else .open = {i:$i, f:$m.f} end)
         else
           (if (($m.f[0:1]) == (.open.f[0:1]))
                and (($m.f|length) >= (.open.f|length))
                and (($m.rest | test("[^[:space:]]")) | not)
            then .prot = (reduce range(.open.i; $i+1) as $j (.prot; .[$j|tostring]=true)) | .open=null
            else . end)
         end)
     | .prot) as $prot
  # --- Pass 2: emit cleaned lines.  dropdepth/depth track <details> nesting so a
  #     NOISE section drops its whole subtree; incmt eats multi-line HTML comments.
  | reduce range(0; $n) as $i ({out:[], incmt:false, dropdepth:-1, depth:0};
      $lines[$i] as $line
      # Eating a multi-line HTML comment takes precedence over fence protection:
      # keep consuming until "-->", then preserve any real text after it.
      | if .incmt then
          (if ($line | test("-->"))
           then (.incmt=false)
                | (($line | sub("^.*?-->"; "")) as $tail
                   | if (.dropdepth < 0 and ($tail | test("[^[:space:]]"))) then (.out += [$tail]) else . end)
           else . end)
        elif ($prot[$i|tostring] == true) then
          (if .dropdepth < 0 then .out += [$line] else . end)
        else
          # Strip an HTML comment only when it OWNS the line (after optional
          # whitespace) — that is how reviewer bots emit their state/fingerprint
          # chrome. An inline "<!--" in prose, or a `<!--` code span, is the
          # author text and is left verbatim (so it never eats real signal).
          ( ($line | test("^\\s*<!--")) as $cmt
            | (if $cmt then ($line | gsub("<!--.*?-->"; "")) else $line end) as $l1
            | (if ($cmt and ($l1 | test("<!--"))) then (.incmt=true) else . end)
            | (if $cmt then ($l1 | gsub("<!--.*$"; "")) else $line end) as $l2
            # A <summary> opens a labelled section at the current depth. Match it
            # whether on its own line or combined as "<details><summary>…</summary>".
            | ($line | test("^\\s*(<details[^>]*>)?<summary>.*</summary>\\s*$")) as $is_summary
            | ($line | test("^\\s*<details[^>]*>\\s*$")) as $is_open
            | ($line | test("^\\s*</details>\\s*$")) as $is_close
            | if ($is_bot and $is_summary) then
                (if ($line | test("^\\s*<details")) then (.depth += 1) else . end)
                | (($line | [capture("<summary>\\s*(?<s>.*?)\\s*</summary>")] | .[0].s
                    | gsub("[*`]"; "") | gsub("^\\s+|\\s+$"; "")) as $lbl
                   | if (.dropdepth < 0 and ($lbl | ascii_downcase | test(NOISE))) then (.dropdepth=.depth)
                     elif (.dropdepth < 0 and ($lbl != "")) then (.out += ["", "**\($lbl)**", ""])
                     else . end)
              elif ($is_bot and $is_open) then (.depth += 1)
              elif ($is_bot and $is_close) then
                (.depth = ([.depth-1, 0] | max))
                | (if (.dropdepth >= 0 and .depth < .dropdepth) then (.dropdepth=-1) else . end)
              else
                (if .dropdepth < 0 then (.out += [$l2]) else . end)
              end)
        end)
  | .out | join("\n")
  | gsub("[ \t]+\n"; "\n") | gsub("\n{3,}"; "\n\n") | gsub("^\\s+|\\s+$"; "");

# HTML-comment strip used only for *metadata* extraction (tag header / title) so a
# leading "<!-- auto-generated by CodeRabbit -->" line cannot hide the tag header.
def strip_comments: gsub("<!--(.*?-->|.*)"; ""; "m");

# Is this line the bot tag header, e.g. "_⚠️ Potential issue_ | _🟠 Major_"? Only
# underscore-delimited tokens joined by "|", each carrying a symbol/emoji. The
# strictness (no prose between tokens) keeps a human line like "_note_ and _ok_ 🔧"
# from being mistaken for a header and stripped. Only ever applied to bot authors.
def is_tag_header($l):
  ($l | test("^\\s*_[^_|]+_(\\s*\\|\\s*_[^_|]+_)*\\s*$")) and ($l | test("\\p{So}"));

def header_tags:
  (split("\n") | map(select(test("[^[:space:]]"))) | .[0] // "") as $first
  | if is_tag_header($first)
    then [ $first | scan("_[^_]+_") | gsub("^_|_$";"") | gsub("^\\s+|\\s+$";"") ]
    else [] end;

def sev_text($t):                   # severity from structured tag text (emoji or word)
  ($t | ascii_downcase) as $x
  | if   ($x|test("critical|🔴")) then "critical"
    elif ($x|test("major|🟠"))    then "major"
    elif ($x|test("minor|🟡|🔵")) then "minor"
    elif ($x|test("nitpick|🧹"))  then "nitpick"
    else "other" end;

def sev_of($tags): sev_text($tags | join(" "));

# Fallback for bots that lead with a markdown heading ("## 🔴 Logic Error")
# instead of an italic tag line: classify by the severity emoji on the first
# non-empty line only (emoji, not words, to avoid prose false positives).
def sev_first:
  (split("\n") | map(select(test("[^[:space:]]"))) | .[0] // "") as $l
  | if   ($l|test("🔴")) then "critical"
    elif ($l|test("🟠")) then "major"
    elif ($l|test("🟡|🔵")) then "minor"
    elif ($l|test("🧹")) then "nitpick"
    else "other" end;

def sev_rank($s):
  {critical:0, major:1, minor:2, nitpick:3, other:4}[$s] // 5;

def title_of:                       # first **bold** line, else first line; sans markup
  (split("\n") | map(select(test("[^[:space:]]")))) as $lines
  | (($lines | map(select(test("\\*\\*"))) | .[0]) // ($lines[0] // ""))
  | gsub("<[^>]+>";"") | gsub("\\*\\*";"") | gsub("^#+\\s*";"") | gsub("^\\s+|\\s+$";"");

def strip_header_line:              # drop the "_…_ | _…_" line from displayed prose
  split("\n")
  | if (length>0 and is_tag_header(.[0]))
    then .[1:] else . end
  | join("\n") | gsub("^\\s+|\\s+$";"");

($threads.data.repository.pullRequest.reviewThreads.nodes // []) as $nodes
| [ $nodes[]
    | (.comments.nodes // []) as $cs
    | ($cs[0] // {}) as $root
    | ($root.body // "") as $rb
    | is_bot($root.author.login) as $root_is_bot
    | ($rb | strip_comments) as $rb_meta
    | ($rb | clean($root_is_bot)) as $root_clean
    | (if $root_is_bot then ($rb_meta | header_tags) else [] end) as $tags
    | {
        resolved: .isResolved,
        outdated: .isOutdated,
        path: .path,
        line: (.line // .startLine),
        reply_to: $root.databaseId,
        root_author: ($root.author.login // "?"),
        is_bot: $root_is_bot,
        tags: $tags,
        severity: (
          if ($tags|length) > 0 then sev_of($tags)
          elif $root_is_bot then ($rb_meta | sev_first)
          else "other" end),
        quick_win: ($tags | any(ascii_downcase | test("quick win"))),
        title: ($root_clean | title_of),
        comments: [ $cs[]
          | is_bot(.author.login) as $cbot
          | {
              author: (.author.login // "?"),
              body: ( (.body // "")
                      | clean($cbot)
                      | (if $cbot then strip_header_line else . end) )
            } ]
      }
  ] as $all
| ($all | map(select(.resolved | not))) as $open
| ($all | map(select(.resolved)))       as $done
| {
    pr: $pr, repo: $repo,
    threads_total: ($all|length),
    unresolved: ($open|length),
    resolved: ($done|length),
    by_severity: ( ["critical","major","minor","nitpick","other"]
                   | map(. as $s | {key:$s, value: ($open | map(select(.severity==$s)) | length)})
                   | map(select(.value>0)) | from_entries ),
    findings: ($open | sort_by(sev_rank(.severity))),
    resolved_findings: $done
  }
'

data="$(jq -n \
  --argjson threads "$threads" \
  --arg repo "$owner/$name" \
  --argjson pr "$pr" \
  --argjson full "$full" \
  "$jq_prog" 2>/dev/null)" || true

if [ -z "$data" ]; then
  echo '{"error":"failed to parse review threads"}'
  exit 0
fi

# --- Reviewer-bot summary verdict (actionable count) extracted in-script ---
verdict="$(printf '%s' "$summary" | jq -r '
  (map(select(((.user.login // "") | ascii_downcase) | (test("\\[bot\\]$") or test("coderabbit|sourcery|qodo|greptile|ellipsis"))))) as $b
  | ($b | map(.body) | reverse) as $bodies
  | ( [ $bodies[] | capture("Actionable comments posted:\\s*(?<n>[0-9]+)";"i").n ] | .[0] ) as $n
  | ( [ $bodies[] | select(test("No actionable comments were generated";"i")) ] | length ) as $none
  | if $n != null then "Actionable comments posted: \($n)"
    elif $none > 0 then "No actionable comments were generated"
    else "" end' 2>/dev/null)" || true

if [ "$want_json" -eq 1 ]; then
  printf '%s' "$data" | jq --arg verdict "$verdict" '. + {actionable_verdict: $verdict}'
  exit 0
fi

# --- Render Markdown ---
printf '%s' "$data" | jq -r --arg verdict "$verdict" --argjson all "$want_all" '
  def sev_icon($s): {critical:"🔴",major:"🟠",minor:"🟡",nitpick:"🧹",other:"•"}[$s] // "•";
  "# Review comments — PR #\(.pr)  (\(.repo))",
  "",
  ( [ "\(.unresolved) unresolved",
      "\(.resolved) resolved"
    ] + (if $verdict != "" then [$verdict] else [] end)
    | join(" · ") ),
  ( if (.by_severity | length) > 0
    then "severity: " + ([ .by_severity | to_entries[] | "\(.key) \(.value)" ] | join(" · "))
    else empty end ),
  "",
  ( if (.findings | length) == 0
    then "_No unresolved review threads._"
    else ( .findings | to_entries[] |
        .key as $i | .value as $f |
        "## \(sev_icon($f.severity)) \($i+1). \($f.path // "?"):\($f.line // "?")"
        + (if $f.quick_win then "  ⚡" else "" end),
        ( [ ($f.tags | if length>0 then join(" · ") else empty end),
            (if $f.is_bot then empty else "by @\($f.root_author)" end) ]
          | map(select(. != null)) | if length>0 then "_" + join(" · ") + "_" else empty end ),
        "",
        ( $f.comments | to_entries[] |
          if .key == 0 then .value.body
          else "\n> **@\(.value.author):** " + (.value.body | gsub("\n";"\n> ")) end ),
        "",
        "↳ reply-to id: `\($f.reply_to)`",
        "",
        "---"
      ) end ),
  ( if $all and (.resolved_findings | length) > 0
    then "",
         "<details><summary>\(.resolved_findings|length) resolved</summary>",
         ( .resolved_findings[] | "\n- \(.path // "?"):\(.line // "?") — \(.title)" ),
         "</details>"
    else empty end )
'
