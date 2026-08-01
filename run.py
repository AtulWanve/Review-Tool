#!/usr/bin/env python3
"""
Review-loop orchestrator.

Runs CodeRabbit on the current changes, then works through the findings ONE FILE
AT A TIME, each file in a fresh coding-agent process (isolated context), gates each
change with a fast static check, and records a plain-language work log you can turn
into a daily standup message.

It NEVER runs git. It leaves the working tree edited in place for you to review and
mirror. State lives on disk so a run interrupted by a usage limit can be resumed.

Commands:
    python run.py review        # run a bounded batch (default)
    python run.py review --fresh # ignore any resumable queue, start a new cycle
    python run.py standup        # build today's progress message from the work log
    python run.py standup --all  # progress message across all logged work
    python run.py print-review   # dump raw CodeRabbit output (calibration / debugging)
    python run.py status         # show queue + today's tallies

See README.md for setup and the stop conditions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = TOOL_DIR / "config.json"
PROMPT_PATH = TOOL_DIR / "prompts" / "worker.md"
STATE_DIR = TOOL_DIR / "state"
BACKUP_DIR = STATE_DIR / "backups"
REASONING_DIR = STATE_DIR / "reasoning"
PROGRESS_LOG = STATE_DIR / "progress.jsonl"
QUEUE_PATH = STATE_DIR / "queue.json"
PROCESSED_PATH = STATE_DIR / "processed.json"
LAST_REVIEW = STATE_DIR / "last_review.json"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def find_repo_root() -> Path:
    p = Path.cwd()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    # Fall back to CWD if no .git found
    return Path.cwd()


REPO_ROOT = find_repo_root()


def win_to_wsl_path(p) -> str:
    r"""C:\Users\x  ->  /mnt/c/Users/x"""
    s = str(Path(p).resolve())
    if len(s) >= 2 and s[1] == ":":
        return "/mnt/" + s[0].lower() + s[2:].replace("\\", "/")
    return s.replace("\\", "/")


def wsl_to_win(token: str) -> str:
    """/mnt/c/Users/x  ->  C:/Users/x   (leaves non-/mnt paths unchanged)"""
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", token)
    return f"{m.group(1).upper()}:/{m.group(2)}" if m else token


def resolve_cmd(cmd: list[str]) -> list[str]:
    """Resolve argv[0] against PATH so Windows .cmd/.exe shims work with shell=False."""
    exe = shutil.which(cmd[0])
    if not exe:
        raise FileNotFoundError(cmd[0])
    return [exe] + cmd[1:]


def run_capture(cmd: list[str], *, stdin: str | None = None, timeout: int | None = None,
                cwd: Path | None = None) -> subprocess.CompletedProcess:
    # When we pipe nothing in, the child would INHERIT our stdin (a TTY) and some CLIs
    # then block forever waiting on it. Hand them /dev/null instead.
    extra = {} if stdin is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(
        resolve_cmd(cmd),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",     # not the Windows cp1252 locale — prompts/output carry → — etc.
        errors="replace",
        timeout=timeout,
        cwd=str(cwd or REPO_ROOT),
        **extra,
    )


def c(text: str, color: str) -> str:
    codes = {"grey": "90", "green": "32", "yellow": "33", "red": "31", "cyan": "36", "bold": "1"}
    if not sys.stdout.isatty():
        return text
    return f"\033[{codes.get(color, '0')}m{text}\033[0m"


def ensure_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# CodeRabbit: run + parse
# --------------------------------------------------------------------------- #
@dataclass
class FileReview:
    file: str            # repo-relative path
    text: str            # verbatim review text for this file (what the worker analyses)
    count: int = 1       # rough number of comment lines, for display / ranking tiebreak
    severity: str = ""   # only the CLI (--agent) supplies this; the extension paste does not


def get_review(cfg: dict, review_file: str | None = None) -> str:
    """Return the raw review text by running the CodeRabbit CLI."""
    cr = cfg["coderabbit"]
    inner = list(cr["cmd"])
    if cr.get("dir"):
        inner += ["--dir", cr["dir"]]
    if cr.get("use_wsl"):
        wsl_cwd = win_to_wsl_path(REPO_ROOT)
        joined = " ".join(shlex.quote(a) for a in inner)
        cmd = ["wsl", "bash", "-lc", f"cd {shlex.quote(wsl_cwd)} && {joined} </dev/null"]
        missing_hint = "WSL not found. Install WSL and CodeRabbit inside it."
    else:
        cmd = inner
        missing_hint = f"CodeRabbit CLI not found ('{inner[0]}')."
    
    try:
        proc = run_capture(cmd, timeout=cr.get("timeout_sec", 900))
    except FileNotFoundError:
        sys.exit(c(missing_hint, "red"))
    
    raw = proc.stdout or ""
    if not raw.strip() and proc.stderr:
        raw = proc.stderr
    if proc.returncode != 0 and not raw.strip():
        where = "inside WSL" if cr.get("use_wsl") else ""
        sys.exit(c(f"CodeRabbit exited {proc.returncode} with no output. Check auth {where}.", "red"))
    return raw


# Segment the pasted review into one verbatim block PER FILE. The VS Code extension does
# NOT tag severities, so we do not rely on severity words — we only detect file headers
# and keep each file's review text verbatim; the worker (with your analysis prompt) reasons
# over the raw text.
#
# CALIBRATION POINT: `_file_header()` decides what counts as a file-header line. Run
# `python run.py print-review` on a real pasted review; if files are mis-split (or none are
# detected), this is the single function to adjust to your format.
SEVERITY_KEYWORDS = [
    "critical", "potential issue", "issue", "warning", "refactor", "suggestion", "nitpick",
]


def _looks_like_repo_file(token: str) -> str | None:
    token = token.strip().strip("`'\"")
    token = re.sub(r":\d+.*$", "", token)   # drop a trailing :line (a /mnt path has no colon)
    token = wsl_to_win(token).lstrip("./")  # /mnt/c/... -> C:/...
    if not token:
        return None
    try:
        p = Path(token)
        if p.is_absolute():                 # make repo-relative if it's inside the repo
            token = str(p.resolve().relative_to(REPO_ROOT)).replace("\\", "/")
        if (REPO_ROOT / token).is_file():
            return token.replace("\\", "/")
    except (ValueError, OSError):
        return None
    return None


def _file_header(line: str) -> str | None:
    """Return the repo file if this line is essentially JUST a file path (a header), else
    None. Conservative on purpose: a line carrying real prose is treated as a comment, not
    a header, so multi-line suggestions are not mis-split."""
    s = re.sub(r"^\W*(file|path|in)\b\s*[:.]?\s*", "", line.strip(), flags=re.I)
    s = s.strip().strip("`'\"")
    rf = _looks_like_repo_file(s)
    if not rf:
        return None
    residual = re.sub(r"(?i)line|[:\s\-–—0-9()#*>·•]", "", s.replace(rf, "", 1))
    return rf if len(residual) <= 2 else None


def _blocks_to_reviews(blocks: dict) -> list[FileReview]:
    out: list[FileReview] = []
    for f, lines in blocks.items():
        seen: set = set()
        kept: list[str] = []
        for ln in lines:                      # drop exact-duplicate comment lines
            key = ln.strip().lower()
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            kept.append(ln)
        text = "\n".join(kept).strip()
        if text:
            out.append(FileReview(file=f, text=text,
                                  count=sum(1 for ln in kept if ln.strip())))
    return out


# A finding written inline, e.g.  "In @Pages/Helm/ReleasesPage.py around lines 968 - 981, ..."
# The path is @-prefixed and repo-relative. This is the format the VS Code extension paste uses.
_AT_PATH_RE = re.compile(r"@([^\s,`'\"()]+)")


def _inline_finding_file(line: str) -> str | None:
    """Return the repo file referenced by the first @path token that resolves, else None."""
    for m in _AT_PATH_RE.finditer(line):
        rf = _looks_like_repo_file(m.group(1))
        if rf:
            return rf
    return None


# CodeRabbit's own severity scale (from `review --agent`), most severe first.
CR_SEVERITY_ORDER = ["critical", "major", "minor", "warning", "info", "nitpick"]


def _best_severity(values: list[str]) -> str:
    ranked = sorted(values, key=lambda s: CR_SEVERITY_ORDER.index(s)
                    if s in CR_SEVERITY_ORDER else len(CR_SEVERITY_ORDER))
    return ranked[0] if ranked else ""


def _parse_ndjson_review(raw: str) -> list[FileReview] | None:
    """`coderabbit review --agent` emits NDJSON — one JSON object per line:

        {"type":"review_context",...}      {"type":"status",...}   {"type":"heartbeat",...}
        {"type":"finding","severity":"major","fileName":"a/b.py",
         "codegenInstructions":"...In @a/b.py around lines 1 - 2, <desc>","suggestions":[]}
        {"type":"complete","status":"review_completed","findings":3}

    Returns None when the input isn't NDJSON, so the caller falls back to text parsing.
    Returns [] when the review completed cleanly with zero findings.
    """
    objs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict) and "type" in o:
            objs.append(o)
    if not objs:
        return None  # not NDJSON

    blocks: dict = {}
    sevs: dict = {}
    for o in objs:
        if o.get("type") != "finding":
            continue
        f = o.get("fileName") or o.get("file")
        body = (o.get("codegenInstructions") or o.get("body") or "").strip()
        if not f or not body:
            continue
        rf = _looks_like_repo_file(str(f)) or str(f).replace("\\", "/")
        # extend line-wise so _blocks_to_reviews collapses the repeated boilerplate line
        blocks.setdefault(rf, []).extend(body.splitlines())
        if o.get("severity"):
            sevs.setdefault(rf, []).append(str(o["severity"]).strip().lower())

    if not blocks:
        # A 'complete' line with no findings means the review really is clean.
        return [] if any(o.get("type") == "complete" for o in objs) else None

    reviews = _blocks_to_reviews(blocks)
    for r in reviews:
        r.severity = _best_severity(sevs.get(r.file, []))
    return reviews


def parse_review(raw: str, cfg: dict) -> list[FileReview]:
    nd = _parse_ndjson_review(raw)          # CodeRabbit CLI `--agent`
    if nd is not None:
        return nd

    stripped = raw.strip()
    if stripped[:1] in "[{":
        try:
            return _parse_json_review(json.loads(stripped))
        except (json.JSONDecodeError, KeyError, TypeError):
            pass  # fall through to text parsing

    blocks: dict = {}                         # insertion order == paste order
    current: str | None = None
    for rawline in raw.splitlines():
        s = rawline.strip()
        if not s:
            continue
        inline = _inline_finding_file(s)      # "In @path/file.py around lines .., <desc>"
        if inline:
            blocks.setdefault(inline, []).append(s)
            continue                          # do NOT set current — keeps boilerplate lines out
        header = _file_header(s)              # fallback: a line that is essentially just a path
        if header:
            current = header
            blocks.setdefault(current, [])
            continue
        if current is not None:
            blocks[current].append(rawline.rstrip())
    return _blocks_to_reviews(blocks)


def _parse_json_review(data) -> list[FileReview]:
    items = data if isinstance(data, list) else data.get("findings") or data.get("comments") or []
    blocks: dict = {}
    for it in items:
        f = it.get("file") or it.get("path") or it.get("filename")
        if not f:
            continue
        rf = _looks_like_repo_file(str(f)) or str(f).replace("\\", "/")
        line = it.get("line") or it.get("line_number")
        body = (it.get("body") or it.get("comment") or it.get("message") or "").strip()
        cat = (it.get("severity") or it.get("category") or it.get("type") or "").strip()
        prefix = " ".join(x for x in (f"[{cat}]" if cat else "",
                                      f"(line {line})" if line else "") if x)
        blocks.setdefault(rf, []).append((prefix + " " + body).strip())
    return _blocks_to_reviews(blocks)


# --------------------------------------------------------------------------- #
# Grouping + ranking (file is the unit of work AND the unit of the cap)
# --------------------------------------------------------------------------- #
def rank_files(reviews: list[FileReview], cfg: dict) -> list[FileReview]:
    """Order the files for this run. The extension doesn't tag severities, so the default
    is PASTE ORDER — you set priority by pasting important files first. Set
    selection.rank_by = "severity" to instead float files whose text mentions a
    severity/category word (critical, potential issue, ...) to the top."""
    if cfg["selection"].get("rank_by", "paste_order") == "severity":
        order = cfg["selection"]["severity_order"]

        def rank(r: FileReview) -> int:
            if r.severity:                       # CLI --agent gives a real severity field
                return order.index(r.severity) if r.severity in order else len(order)
            hits = [order.index(k) for k in order if k in r.text.lower()]
            return min(hits) if hits else len(order)

        return sorted(reviews, key=lambda r: (rank(r), -r.count))
    return list(reviews)  # paste order (dict preserved first-seen order)


# --------------------------------------------------------------------------- #
# The per-file worker (fresh agent process)
# --------------------------------------------------------------------------- #
def build_prompt(review: FileReview) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{file}", review.file).replace("{findings}", review.text)


# Capture everything between ```json fences (stops at the closing fence, so nested
# braces in the summary are preserved).
_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL)


def parse_worker_summary(stdout: str) -> dict | None:
    blocks = [b.strip() for b in _JSON_BLOCK_RE.findall(stdout)]
    if not blocks:
        # last-ditch: a bare {...} at the end
        m = re.search(r"(\{.*\})\s*$", stdout, re.DOTALL)
        blocks = [m.group(1)] if m else []
    for block in reversed(blocks):
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            continue
    return None


class LimitReached(Exception):
    pass


def _save_reasoning(file: str, prompt: str, stdout: str) -> None:
    """Persist the agent's full analysis + actions so you can audit its reasoning later —
    one readable file per session under state/reasoning/."""
    REASONING_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now()
    dst = REASONING_DIR / f"{ts.strftime('%Y%m%d-%H%M%S')}__{file.replace('/', '__')}.md"
    dst.write_text(
        f"# {file}\n_{ts.isoformat(timespec='seconds')}_\n\n"
        f"## Prompt sent to the agent\n\n{prompt}\n\n"
        f"## Agent analysis + actions\n\n{stdout}\n",
        encoding="utf-8",
    )


def run_worker(cfg: dict, review: FileReview) -> dict:
    agent = cfg["agent"]
    prompt = build_prompt(review)
    try:
        proc = run_capture(agent["cmd"], stdin=prompt, timeout=agent.get("timeout_sec", 600))
    except FileNotFoundError:
        sys.exit(c(f"Coding agent not found ('{agent['cmd'][0]}'). Fix agent.cmd in config.json.", "red"))
    _save_reasoning(review.file, prompt, proc.stdout or "")
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    low = out.lower()
    if any(marker in low for marker in agent.get("limit_markers", [])):
        raise LimitReached()
    summary = parse_worker_summary(proc.stdout or "")
    if summary is None:
        summary = {"file": review.file, "applied": [], "skipped": [], "_no_summary": True}
    summary.setdefault("applied", [])
    summary.setdefault("skipped", [])
    summary["_agent_rc"] = proc.returncode
    return summary


# --------------------------------------------------------------------------- #
# The static gate
# --------------------------------------------------------------------------- #
def gate(cfg: dict, file: str) -> tuple[bool, str]:
    g = cfg["gate"]
    path = REPO_ROOT / file
    if not path.is_file():
        return True, "file missing (skipped gate)"
    if g.get("py_compile", True):
        proc = run_capture([sys.executable, "-m", "py_compile", str(path)])
        if proc.returncode != 0:
            return False, "py_compile: " + (proc.stderr or proc.stdout).strip()
    if g.get("ruff", True) and shutil.which("ruff"):
        proc = run_capture(["ruff", "check", "--select", g.get("ruff_select", "E9,F63,F7,F82"),
                            str(path)])
        if proc.returncode != 0:
            return False, "ruff: " + (proc.stdout or proc.stderr).strip()
    return True, "ok"


def gate_crossfile(cfg: dict, files: list[str]) -> tuple[bool, str]:
    if not (cfg["gate"].get("end_of_run_crossfile", True) and shutil.which("ruff") and files):
        return True, "ok"
    paths = [str(REPO_ROOT / f) for f in files if (REPO_ROOT / f).is_file()]
    if not paths:
        return True, "ok"
    proc = run_capture(["ruff", "check", "--select",
                        cfg["gate"].get("ruff_select", "E9,F63,F7,F82"), *paths])
    return proc.returncode == 0, (proc.stdout or proc.stderr).strip()


# --------------------------------------------------------------------------- #
# State: backups, queue, progress log
# --------------------------------------------------------------------------- #
def backup_file(file: str) -> Path | None:
    src = REPO_ROOT / file
    if not src.is_file():
        return None
    dst = BACKUP_DIR / file.replace("/", "__")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def restore_file(file: str, backup: Path | None) -> None:
    if backup and backup.is_file():
        shutil.copy2(backup, REPO_ROOT / file)


def append_progress(entry: dict) -> None:
    entry["ts"] = datetime.now().isoformat(timespec="seconds")
    with open(PROGRESS_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def save_queue(q: dict) -> None:
    QUEUE_PATH.write_text(json.dumps(q, indent=2), encoding="utf-8")


def load_queue() -> dict | None:
    if QUEUE_PATH.is_file():
        try:
            return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def clear_queue() -> None:
    if QUEUE_PATH.is_file():
        QUEUE_PATH.unlink()


# Cross-run progress for ONE pasted review: which files are finished, so re-runs advance
# to the next batch instead of redoing the same files. Keyed by a hash of the review text,
# so pasting a new review (or --fresh) resets it automatically. The review file is never modified.
def _review_hash(raw: str) -> str:
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def load_processed() -> dict:
    if PROCESSED_PATH.is_file():
        try:
            return json.loads(PROCESSED_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"review_hash": None, "done": []}


def save_processed(p: dict) -> None:
    PROCESSED_PATH.write_text(json.dumps(p, indent=2), encoding="utf-8")


def mark_done(file: str) -> None:
    p = load_processed()
    if file not in p["done"]:
        p["done"].append(file)
        save_processed(p)


# --------------------------------------------------------------------------- #
# review command
# --------------------------------------------------------------------------- #
def cmd_review(cfg: dict, args) -> None:
    ensure_state()
    sel = cfg["selection"]

    # Resume?  A leftover queue means a prior run stopped mid-way.
    queue = None if args.fresh else load_queue()
    if queue and queue.get("pending"):
        print(c(f"Resuming previous cycle: {len(queue['pending'])} file(s) left.", "cyan"))
        selected = queue["pending"]
        reviews_by_file = {f: FileReview(file=f, text=queue.get("reviews", {}).get(f, ""),
                                         count=0) for f in selected}
    else:
        print(c("Reading review (CodeRabbit CLI)...", "cyan"))
        raw = get_review(cfg, getattr(args, "review_file", None))
        LAST_REVIEW.write_text(raw, encoding="utf-8")
        reviews = parse_review(raw, cfg)

        # STOP A: nothing parsed at all.
        if not reviews:
            print(c("Nothing to review — no file suggestions found. Done.", "green"))
            clear_queue()
            return

        # Progress across runs: skip files already finished for THIS review text.
        rh = _review_hash(raw)
        proc = load_processed()
        if args.fresh or proc.get("review_hash") != rh:
            proc = {"review_hash": rh, "done": []}
            save_processed(proc)
        done_set = set(proc["done"])
        candidates = [r for r in reviews if r.file not in done_set]

        # STOP A': every file in this review is already done.
        if not candidates:
            print(c(f"All {len(reviews)} file(s) in this review are done - nothing left. Paste a new review, or run `review --fresh` to redo this one.", "green"))
            clear_queue()
            return

        ranked = rank_files(candidates, cfg)
        limit = len(ranked) if getattr(args, "all", False) else sel["max_changed_files"]
        chosen = ranked[: sel["max_examined_files"]][: limit]
        remaining = len(candidates) - len(chosen)
        done_note = f" ({len(done_set)} already done)" if done_set else ""
        print(c(f"{len(candidates)} file(s) left in this review{done_note}; working "
                f"{len(chosen)} now" + (f", {remaining} remain for next run." if remaining else "."), "grey"))
        selected = [r.file for r in chosen]
        reviews_by_file = {r.file: r for r in chosen}

    # -- Execution loop --
    touched_ok = []
    done = []
    for file in selected:
        try:
            print(c(f"\\n→ {file}", "cyan") + f"  ({reviews_by_file[file].count} comment(s))")
            ok_before, _ = gate(cfg, file)
            backup = backup_file(file)
            prompt = build_prompt(reviews_by_file[file])
            summary = run_worker(cfg, reviews_by_file[file])
            if summary.get("error"):
                restore_file(file, backup)
                if summary["error"] in ("limit", "timeout"):
                    print(c(f"Usage/limit signal from the agent - stopping. Re-run to resume.", "yellow"))
                    break
                print(c(f"Agent failed to return a valid JSON summary - skipped.", "red"))
                continue
        except Exception as e:  # noqa: BLE001 — one bad file must not kill the batch
            restore_file(file, backup)  # undo any partial edit
            print(c(f"  error — skipped, kept for a later run: "
                    f"{type(e).__name__}: {str(e)[:200]}", "red"))
            _log_file(file, "error", {"applied": [], "skipped": []}, f"{type(e).__name__}: {e}")
            continue  # not marked done -> a later `review` run retries it

        ok_after, detail = gate(cfg, file)

        if ok_after or not ok_before:
            # kept: either it passed, or the file was already red (don't blame the worker)
            action = "applied" if summary["applied"] else ("skipped" if summary["skipped"] else "no-op")
            if not ok_after and not ok_before:
                action += " (gate still red, pre-existing)"
            touched_ok.append(file) if summary["applied"] else None
            consecutive_failures = 0
            _log_file(file, action, summary, detail)
            _print_file_result(action, summary)
        else:
            # regressed: worker broke a file that was clean -> revert
            restore_file(file, backup)
            consecutive_failures += 1
            _log_file(file, "reverted", summary, detail)
            print(c(f"  reverted — fix broke the gate: {detail[:200]}", "red"))
            if consecutive_failures >= cfg["safety"]["consecutive_gate_failures_abort"]:
                print(c(f"\nAborting: {consecutive_failures} fixes broke the build in a row. "
                        f"Something's off — take a look.", "red"))
                done.append(file)
                mark_done(file)
                _persist_pending(selected, done)
                _report(touched_ok)
                return

        done.append(file)
        mark_done(file)
        _persist_pending(selected, done)

    _report(touched_ok)

    # finished the batch
    ok, detail = gate_crossfile(cfg, touched_ok)
    if not ok:
        print(c(f"\nHeads-up: cross-file static check flagged something after the batch — "
                f"worth a look:\n{detail[:500]}", "yellow"))
    clear_queue()
    _report(touched_ok)


def _persist_pending(selected: list[str], done: list[str]) -> None:
    q = load_queue() or {}
    q["pending"] = [f for f in selected if f not in done]
    save_queue(q)


def _log_file(file: str, action: str, summary: dict, gate_detail: str) -> None:
    append_progress({
        "file": file,
        "action": action,
        "applied": summary.get("applied", []),
        "skipped": summary.get("skipped", []),
        "gate": gate_detail[:300],
    })


def _print_file_result(action: str, summary: dict) -> None:
    for a in summary.get("applied", []):
        print(c(f"  ✓ {a.get('summary', '(no description)')}", "green"))
    for s in summary.get("skipped", []):
        print(c(f"  – skipped: {s.get('reason', '')}", "grey"))
    if summary.get("_no_summary"):
        print(c("  (agent returned no structured summary — kept changes, gate passed)", "grey"))


def _report(touched_ok: list[str]) -> None:
    n = len(touched_ok)
    print(c(f"\nDone. {n} file(s) changed this run.", "cyan"))
    print(c("Working tree left edited (no commits). "
            "Progress saved to state/progress.jsonl.", "grey"))


# --------------------------------------------------------------------------- #
# standup command  (code-generated, zero agent tokens)
# --------------------------------------------------------------------------- #
def _iter_progress():
    if not PROGRESS_LOG.is_file():
        return
    for line in PROGRESS_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def cmd_print_review(cfg: dict, args) -> None:
    raw = get_review(cfg, getattr(args, "review_file", None))
    print(raw)
    print(c("\n--- parsed as (file -> its review text) ---", "cyan"))
    reviews = parse_review(raw, cfg)
    if not reviews:
        print(c("  (no files detected — _file_header() needs calibrating to this format)", "yellow"))
    for r in reviews:
        print(c(f"\n{r.file}  ({r.count} comment line(s))", "bold"))
        for ln in r.text.splitlines():
            if ln.strip():
                print(f"    {ln}")


def cmd_fetch(cfg: dict, args) -> None:
    """Run ONE CodeRabbit review and save it, so you can then work through it a file at a
    time without burning a review (and a rate-limit slot) on every run."""
    ensure_state()
    forced = {**cfg, "coderabbit": {**cfg["coderabbit"], "source": "cli"}}
    scope = cfg["coderabbit"].get("dir") or "whole repo"
    print(c(f"Running CodeRabbit ({scope})... this can take minutes.", "cyan"))
    raw = get_review(forced, None)
    out = Path(args.out) if args.out else (STATE_DIR / "last_review.ndjson")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(raw, encoding="utf-8")

    reviews = parse_review(raw, cfg)
    if not reviews:
        print(c("Review completed with no findings — nothing to fix.", "green"))
        return
    print(c(f"\nSaved review -> {out}", "green"))
    print(f"{len(reviews)} file(s) with findings, {sum(r.count for r in reviews)} comment line(s):")
    for r in rank_files(reviews, cfg):
        print(f"  [{r.severity or '-':<8}] {r.file}")
    print(c(f"\nNow work it one file per run:\n  python run.py review --review-file {out}", "cyan"))


def cmd_status(cfg: dict, args) -> None:
    q = load_queue()
    if q and q.get("pending"):
        print(c(f"Resumable cycle: {len(q['pending'])} file(s) pending — {', '.join(q['pending'])}", "yellow"))
    else:
        print(c("No resumable cycle.", "grey"))
    proc = load_processed()
    if proc.get("done"):
        print(c(f"Current review: {len(proc['done'])} file(s) processed so far "
                f"(re-runs skip these; a new paste or --fresh resets).", "grey"))
    today = date.today().isoformat()
    applied = reverted = skipped = 0
    for e in _iter_progress():
        if not str(e.get("ts", "")).startswith(today):
            continue
        applied += len(e.get("applied", []))
        skipped += len(e.get("skipped", []))
        reverted += 1 if e.get("action", "").startswith("reverted") else 0
    print(f"Today: {applied} change(s) applied, {skipped} skipped, {reverted} file(s) reverted by gate.")


# --------------------------------------------------------------------------- #
def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # avoid UnicodeEncodeError on legacy Windows consoles
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="CodeRabbit -> agent review loop (no git, bounded).")
    sub = parser.add_subparsers(dest="command")
    p_rev = sub.add_parser("review", help="run a bounded review/fix batch (default)")
    p_rev.add_argument("--fresh", action="store_true", help="ignore any resumable queue")
    p_rev.add_argument("--all", action="store_true", help="loop until every file in the queue is done")
    p_rev.add_argument("--review-file", dest="review_file", default=None,
                       help="read the review from this file instead of the CLI")

    p_pr = sub.add_parser("print-review", help="dump the raw review (calibration)")
    p_pr.add_argument("--review-file", dest="review_file", default=None,
                      help="read the review from this file instead of the CLI")

    p_f = sub.add_parser("fetch", help="run ONE CodeRabbit review and save it for repeated use")
    p_f.add_argument("--out", default=None, help="where to save (default state/last_review.ndjson)")

    sub.add_parser("status", help="show queue + today's tallies")

    args = parser.parse_args()
    cfg = load_config()
    cmd = args.command or "review"
    if cmd == "review":
        if not hasattr(args, "fresh"):
            args.fresh = False
        cmd_review(cfg, args)
    elif cmd == "print-review":
        cmd_print_review(cfg, args)
    elif cmd == "fetch":
        cmd_fetch(cfg, args)
    elif cmd == "status":
        cmd_status(cfg, args)


if __name__ == "__main__":
    main()
