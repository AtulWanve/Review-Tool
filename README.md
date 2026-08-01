# review-loop

Automates the manual cycle of *"CodeRabbit reviews → I copy the suggestions → paste
into a coding agent → repeat."* It runs CodeRabbit on your current changes and works
through the findings **one file at a time**, each file in a **fresh agent process** so
context never piles up, gates every change with a fast static check, and writes a
plain-language work log you can turn into a daily standup message.

It **never runs git.** It leaves the working tree edited in place for you to review and
mirror to your second repo. Nothing is committed, staged, or pushed.

---

## CLI mode (the default — free OAuth login, no paste)

The CodeRabbit CLI is Linux/macOS-only, so on Windows it runs inside **WSL** — only the
review *read* crosses the boundary. Auth once with the **free OAuth login** (an
`--api-key` needs a paid *agentic* key; you don't need that):

```bash
# inside WSL, once:
coderabbit auth login && coderabbit auth status
```

The tool runs `coderabbit review --agent`, which emits **NDJSON** — one JSON object per
line, with `finding` objects carrying `severity`, `fileName`, and `codegenInstructions`.
That gives real severities, so `selection.rank_by` is `"severity"`.

### The workflow — fetch once, then work it a file at a time

`review` with `source:"cli"` triggers a **new** CodeRabbit review every run. At
`max_changed_files: 1` that's one review per file — slow and rate-limit hungry. Instead:

```bash
python run.py fetch                                       # ONE review, saved + listed
python run.py review --review-file state/last_review.ndjson   # 1 file per run, repeat
python run.py standup                                     # the Slack message
```

Progress is tracked against that saved review's hash, so each run advances to the next
file and never redoes one. When it prints *"all files done"*, `fetch` again to re-review.

> ⚠️ **A whole-repo review of a very large diff can stall for hours.** Scope it with
> `coderabbit.dir` (e.g. `"Utils"`, `"Services/kubernetes"`) or add `--light` to
> `coderabbit.cmd`. A scoped review finishes in minutes.

### Paste mode (no CLI needed)

Set `coderabbit.source` to `"file"` and paste a review into `review.txt` (the extension's
copy output — which is just the `codegenInstructions` text — parses fine). Same pipeline,
no CodeRabbit auth or rate limit. Progress tracking works identically.

Prereqs either way: **ruff** in your `.venv` (the gate) and **Claude Code** (`claude` on
PATH, the default agent — change `agent.cmd` to swap tools). On native Mac/Linux set
`coderabbit.use_wsl` to `false`.

---

## Review format (already calibrated)

The parser is tuned to the format your paste uses: one finding per line, e.g.

```
In @Pages/Helm/ReleasesPage.py around lines 968 - 981, <description...>
```

It reads the `@`-prefixed repo path from each such line (ignoring the preamble and the
repeated "Verify each finding…" boilerplate), and groups every finding for the same file
together. Multiple findings for one file → one session. To see how any paste parses:

```bash
python run.py print-review
```

If the format ever changes, the calibration points are `_inline_finding_file()` (the
`In @path …` lines) and `_file_header()` (a fallback "path on its own line" layout).

---

## Usage

```bash
python run.py review        # run one bounded batch (the default)
python run.py review --fresh # ignore any resumable queue and start a new cycle
python run.py standup        # build TODAY'S progress message from the log
python run.py standup --all  # progress message across everything logged
python run.py status         # show the resumable queue + today's tallies
python run.py print-review   # calibration / debugging
```

Typical day: `python run.py review`, eyeball the changes, mirror to the second repo,
then `python run.py standup`, read the message, paste it to Slack.

---

## What one `review` run does

1. Reads the review and **splits it by file** (verbatim text per file; the same file
   appearing in several pasted reviews is merged, duplicate lines dropped).
2. **Priority:** the extension doesn't tag severities, so files are taken in **paste
   order** — you control priority by pasting important files first. It takes the top
   `max_changed_files` (**the "day's work" cap** — the unit is *files*, so a file is never
   split across runs). Set `selection.rank_by = "severity"` to instead float files whose
   text mentions critical/potential issue/etc.
3. For each file: backs it up → hands **all** of that file's suggestions to **one fresh
   agent process** (isolated context, tokens not wasted re-loading). The agent runs your
   **7-step analysis** on each suggestion (restate → current behavior → proposed fix →
   result → necessity → risks → Apply/Do-not-apply/Needs-more-context), then applies only
   the ones it recommends and skips the rest with reasons.
4. **Its full reasoning is saved** to `state/reasoning/<timestamp>__<file>.md` — the prompt
   plus the agent's whole analysis and actions, so you can audit any session later.
5. **Gate:** `py_compile` + `ruff --select E9,F63,F7,F82` on the file. If a fix breaks a
   file that was previously clean, that file is **reverted** and logged. (If the file was
   already broken before we touched it, we don't blame the fix.)
6. Logs each file's applied/skipped items to `state/progress.jsonl`; one cross-file static
   pass after the batch.

**Permissions:** the agent runs headless with `--permission-mode acceptEdits` and
`--allowedTools Read,Edit,Write` (in `agent.cmd`), so it never blocks on a prompt and can't
run shell or git. For zero checks, switch to `--permission-mode bypassPermissions`.

The progress message (`standup`) is generated **from the log by code — zero agent
tokens** — using the plain sentences the agent wrote for each applied change. No mention
of tools, AI, or "review"; flat bullets in your own voice.

---

## Stop conditions (all armed every run)

- **A · Done** — no findings parsed (empty/clean review), **or** every file in the current
  review has already been processed. Prints *"nothing left"* and exits.
- **B · Day cap** — processed `max_changed_files` files; the rest wait for the next run,
  which **skips the finished files** and advances to the next batch (progress is tracked
  per review in `state/processed.json`; a new paste or `--fresh` resets it).
- **C · Safety** —
  - a fix breaks a previously-clean file → that file is reverted (not a full stop);
  - `consecutive_gate_failures_abort` reverts in a row → aborts the run;
  - the agent signals a usage/rate limit (`agent.limit_markers`) → stops and keeps the
    queue so a later run **resumes** where it left off.
- **D · Manual** — Ctrl-C saves progress; re-run to resume.

Because the queue + log live in `state/`, hitting your usage limit mid-run is safe: just
run `review` again after it resets and it continues, skipping finished files.

---

## Config knobs (`config.json`)

| Key | Meaning |
|---|---|
| `coderabbit.source` | `"file"` — read the pasted review (default). `"cli"` — run the CLI (needs an agentic key). |
| `coderabbit.review_file` | Where the pasted review lives (default `review.txt`). |
| `coderabbit.use_wsl` | Only for `source:"cli"` — run the CLI inside WSL on Windows. |
| `agent.cmd` | The coding-agent adapter (prompt on stdin, edits files, prints a ```json summary). Holds the permission flags. |
| `agent.limit_markers` | Strings that mean "usage limit hit" → stop + keep queue for resume. |
| `gate.ruff_select` | Rule set for the static gate. Default = syntax + undefined names. |
| `selection.rank_by` | `"paste_order"` (default) or `"severity"`. See *What one run does*. |
| `selection.max_changed_files` | The day cap (default 8 files/run). |
| `selection.max_examined_files` | Absolute ceiling on files considered in one run. |
| `safety.consecutive_gate_failures_abort` | Runaway guard (default 3). |

---

## Swapping the agent (platform independence)

The only Claude-specific thing is `agent.cmd`. The contract for any replacement:

> Read the prompt on **stdin**, edit files in place, and print a fenced ` ```json ` block
> matching the shape in `prompts/worker.md`.

Point `agent.cmd` at any other agentic CLI that can do that and the loop is unchanged.
Test with Claude first, then switch to keep your main Claude quota free.

---

## Deliberately NOT done

- **No git.** No commit/stage/push. The output is a dirty working tree + `state/` logs.
- **No test suite.** The gate is static-only by design. A real pytest suite (targeting
  `Business_Logic` / `Services` / `Utils`) is a separate initiative.
- **No auto-send to Slack.** `standup` only *generates* the message; you read and paste it.
