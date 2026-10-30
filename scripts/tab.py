#!/usr/bin/env python3
"""token-tab: itemize what a coding agent's sessions spent, point at repeat
waste with evidence, and judge one change with a before/after trial.

Read-only and offline. Python 3.9+ standard library only.

Commands
  bill    itemized tab per session and in total
  tells   ranked waste tells with line-level evidence
  trial   compare sessions before and after one change
  demo    write a synthetic sample (clearly marked) to try the commands on
"""
import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict

VERSION = "1.0.0"
CHARS_PER_TOKEN = 4

DEFAULTS = {
    "loop": 3,          # same failing action within one session
    "relearn": 3,       # same failing action across this many sessions
    "flood": 6000,      # estimated tokens in one tool result
    "reread": 3,        # same file read this many times in one session
    "cache_break": 2000,  # minimum cache-write tokens on a later call
    "heavy_start": 20000,  # context size of the first call
    "long_haul": 120000,   # context size where a session is getting long
    "idle_sessions": 5,    # loaded tool unused across this many sessions
}

READ_TOOLS = {"read", "read_file", "view", "cat", "open_file"}
SHELL_TOOLS = {"bash", "shell", "exec", "run", "terminal", "exec_command", "local_shell"}


# ---------------------------------------------------------------- parsing

def est_tokens(text):
    if not text:
        return 0
    return int(math.ceil(len(text) / CHARS_PER_TOKEN))


def flatten_result(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "image":
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(block, sort_keys=True))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return json.dumps(content, sort_keys=True)


def norm_space(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def action_key(name, args):
    """A stable key for 'the same action': command text, or file path."""
    lname = (name or "").lower()
    if isinstance(args, dict):
        for field in ("command", "cmd"):
            if field in args:
                val = args[field]
                if isinstance(val, list):
                    val = " ".join(str(v) for v in val)
                return lname + ":" + norm_space(val)
        for field in ("file_path", "path", "filename", "target"):
            if field in args:
                return lname + ":" + norm_space(args[field])
        return lname + ":" + json.dumps(args, sort_keys=True)[:200]
    if args is None:
        return lname
    return lname + ":" + norm_space(args)[:200]


def file_of(args):
    if isinstance(args, dict):
        for field in ("file_path", "path", "filename", "target"):
            if field in args:
                return norm_space(args[field])
    if isinstance(args, str):
        return norm_space(args)
    return None


class Session:
    def __init__(self, sid, source):
        self.sid = sid
        self.source = source
        self.calls = []   # dicts: idx line model fresh cache_write cache_read out
        self.tools = []   # dicts: name key file ok result_tokens line call_idx
        self.skipped = 0

    def context(self, c):
        return c["fresh"] + c["cache_write"] + c["cache_read"]

    def total(self, c):
        return c["fresh"] + c["cache_write"] + c["cache_read"] + c["out"]


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def parse_lines(lines, source, default_sid):
    """Parse JSONL lines in Claude Code style or the generic token-tab style.

    Returns a dict sid -> Session.
    """
    sessions = {}
    seen_msg = set()
    pending = {}  # tool_use_id -> tool dict

    def get(sid):
        if sid not in sessions:
            sessions[sid] = Session(sid, source)
        return sessions[sid]

    for lineno, raw in enumerate(lines, 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            get(default_sid).skipped += 1
            continue
        if not isinstance(obj, dict):
            get(default_sid).skipped += 1
            continue

        # generic format
        if obj.get("kind") in ("call", "tool"):
            s = get(str(obj.get("session") or default_sid))
            if obj["kind"] == "call":
                s.calls.append({
                    "idx": len(s.calls), "line": lineno,
                    "model": str(obj.get("model") or "unknown"),
                    "fresh": _int(obj.get("in")),
                    "cache_write": _int(obj.get("cache_write")),
                    "cache_read": _int(obj.get("cache_read")),
                    "out": _int(obj.get("out")),
                })
            else:
                name = str(obj.get("name") or "tool")
                args = obj.get("args")
                rt = obj.get("result_tokens")
                if rt is None:
                    rt = est_tokens(obj.get("result", ""))
                s.tools.append({
                    "name": name, "key": action_key(name, args),
                    "file": file_of(args), "ok": bool(obj.get("ok", True)),
                    "result_tokens": _int(rt), "line": lineno,
                    "call_idx": max(len(s.calls) - 1, 0),
                })
            continue

        # Claude Code style
        msg = obj.get("message")
        typ = obj.get("type")
        if typ in ("assistant", "user") and isinstance(msg, dict):
            s = get(str(obj.get("sessionId") or default_sid))
            content = msg.get("content")
            if typ == "assistant":
                usage = msg.get("usage") or {}
                mid = msg.get("id") or obj.get("requestId")
                # one API response may be logged on several lines; count it once
                if usage and (mid is None or (s.sid, mid) not in seen_msg):
                    if mid is not None:
                        seen_msg.add((s.sid, mid))
                    s.calls.append({
                        "idx": len(s.calls), "line": lineno,
                        "model": str(msg.get("model") or "unknown"),
                        "fresh": _int(usage.get("input_tokens")),
                        "cache_write": _int(usage.get("cache_creation_input_tokens")),
                        "cache_read": _int(usage.get("cache_read_input_tokens")),
                        "out": _int(usage.get("output_tokens")),
                    })
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name = str(block.get("name") or "tool")
                            args = block.get("input")
                            t = {"name": name, "key": action_key(name, args),
                                 "file": file_of(args), "ok": True,
                                 "result_tokens": 0, "line": lineno,
                                 "call_idx": max(len(s.calls) - 1, 0)}
                            s.tools.append(t)
                            if block.get("id"):
                                pending[block["id"]] = t
            else:
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            t = pending.pop(block.get("tool_use_id"), None)
                            if t is None:
                                continue
                            t["ok"] = not bool(block.get("is_error"))
                            t["result_tokens"] = est_tokens(flatten_result(block.get("content")))
            continue
        get(default_sid).skipped += 1
    return sessions


def load_paths(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                for n in sorted(names):
                    if n.endswith(".jsonl"):
                        files.append(os.path.join(root, n))
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise SystemExit("token-tab: no such file or folder: %s" % p)
    sessions = {}
    for f in sorted(files):
        stem = os.path.splitext(os.path.basename(f))[0]
        with open(f, encoding="utf-8", errors="replace") as fh:
            got = parse_lines(fh, f, stem)
        for sid, s in got.items():
            if sid in sessions:
                base = sessions[sid]
                off = len(base.calls)
                for c in s.calls:
                    c["idx"] += off
                    base.calls.append(c)
                for t in s.tools:
                    t["call_idx"] += off
                    base.tools.append(t)
                base.skipped += s.skipped
            else:
                sessions[sid] = s
    return [s for s in sessions.values() if s.calls or s.tools]


# ---------------------------------------------------------------- prices

def load_prices(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        card = json.load(fh)
    if not card.get("as_of"):
        raise SystemExit("token-tab: price card needs an as_of date (when you checked the prices)")
    models = card.get("models") or {}
    if not models:
        raise SystemExit("token-tab: price card has no models")
    for name, row in models.items():
        for k in ("input", "cache_write", "cache_read", "output"):
            if k not in row:
                raise SystemExit("token-tab: price for %s is missing %s" % (name, k))
    return card


def price_row(card, model):
    if not card:
        return None
    best = None
    for name, row in card["models"].items():
        if model == name or model.startswith(name):
            if best is None or len(name) > len(best[0]):
                best = (name, row)
    return best[1] if best else None


def call_cost(card, c):
    row = price_row(card, c["model"])
    if row is None:
        return None
    return (c["fresh"] * row["input"] + c["cache_write"] * row["cache_write"]
            + c["cache_read"] * row["cache_read"] + c["out"] * row["output"]) / 1e6


# ---------------------------------------------------------------- bill

def bill(sessions, card=None):
    rows = []
    tot = Counter()
    unpriced = set()
    for s in sessions:
        r = Counter()
        cost = 0.0
        priced = True
        for c in s.calls:
            for k in ("fresh", "cache_write", "cache_read", "out"):
                r[k] += c[k]
            if card:
                v = call_cost(card, c)
                if v is None:
                    priced = False
                    unpriced.add(c["model"])
                else:
                    cost += v
        r["calls"] = len(s.calls)
        r["tool_results"] = sum(t["result_tokens"] for t in s.tools)
        r["start"] = s.context(s.calls[0]) if s.calls else 0
        r["peak"] = max((s.context(c) for c in s.calls), default=0)
        total = r["fresh"] + r["cache_write"] + r["cache_read"] + r["out"]
        row = {"session": s.sid, "source": s.source, "calls": r["calls"],
               "fresh": r["fresh"], "cache_write": r["cache_write"],
               "cache_read": r["cache_read"], "out": r["out"], "total": total,
               "start_context": r["start"], "peak_context": r["peak"],
               "tool_result_tokens_est": r["tool_results"],
               "cost": round(cost, 4) if (card and priced) else None,
               "skipped_lines": s.skipped}
        rows.append(row)
        for k in ("calls", "fresh", "cache_write", "cache_read", "out"):
            tot[k] += r[k]
        tot["total"] += total
        tot["tool_results"] += r["tool_results"]
        tot["start"] += r["start"]
    total_cost = None
    if card and not unpriced:
        total_cost = round(sum(r["cost"] for r in rows if r["cost"] is not None), 4)
    items = []
    if tot["total"]:
        for label, key in (("re-reading cached context", "cache_read"),
                           ("writing context to cache", "cache_write"),
                           ("fresh input", "fresh"), ("output", "out")):
            items.append({"item": label, "tokens": tot[key],
                          "share": round(tot[key] / tot["total"], 3)})
    rows.sort(key=lambda r: -(r["cost"] if r["cost"] is not None else r["total"]))
    return {"sessions": rows, "items": items,
            "totals": {"sessions": len(rows), "calls": tot["calls"],
                       "tokens": tot["total"],
                       "opening_context_tokens": tot["start"],
                       "tool_result_tokens_est": tot["tool_results"],
                       "cost": total_cost},
            "prices_as_of": card.get("as_of") if card else None,
            "unpriced_models": sorted(unpriced)}


# ---------------------------------------------------------------- tells

def _direct(s, tools):
    """Floor estimate: the tool output fed back plus the output that asked for it."""
    calls = {c["idx"]: c for c in s.calls}
    asked = set(t["call_idx"] for t in tools)
    return sum(t["result_tokens"] for t in tools) + sum(calls[i]["out"] for i in asked if i in calls)


def find_tells(sessions, th=None, loaded=None):
    th = dict(DEFAULTS, **(th or {}))
    tells = []
    fail_sessions = defaultdict(list)   # key -> [(sid, tool)]

    for s in sessions:
        # LOOP
        fails = defaultdict(list)
        for t in s.tools:
            if not t["ok"]:
                fails[t["key"]].append(t)
                fail_sessions[t["key"]].append((s.sid, t))
        for key, ts in fails.items():
            if len(ts) >= th["loop"]:
                tells.append({"code": "LOOP", "session": s.sid, "what": key,
                              "count": len(ts), "lines": [t["line"] for t in ts],
                              "tokens": _direct(s, ts[1:]),
                              "lever": "Stop after the second identical failure: read the error, change approach, or ask. A short rule in the instruction file can say so.",
                              "undo": "Delete that rule."})
        # FLOOD
        later = Counter()
        for t in s.tools:
            if t["result_tokens"] >= th["flood"]:
                carried = max(len(s.calls) - 1 - t["call_idx"], 0)
                tells.append({"code": "FLOOD", "session": s.sid, "what": t["key"],
                              "count": 1, "lines": [t["line"]],
                              "tokens": t["result_tokens"] * max(carried, 1),
                              "lever": "Ask for less: filter, head, grep, a line range, quieter flags, or a summary step before the output enters context. It is re-read on every later call (%d here)." % carried,
                              "undo": "Drop the filter."})
        # REREAD
        reads = defaultdict(list)
        for t in s.tools:
            if t["name"].lower() in READ_TOOLS and t["file"]:
                reads[t["file"]].append(t)
        for f, ts in reads.items():
            if len(ts) >= th["reread"]:
                tells.append({"code": "REREAD", "session": s.sid, "what": f,
                              "count": len(ts), "lines": [t["line"] for t in ts],
                              "tokens": sum(t["result_tokens"] for t in ts[1:]),
                              "lever": "Read the needed range once, or keep a short note of what the file says; reread only after it changed.",
                              "undo": "Nothing to undo; it is a habit."})
        # CACHE_BREAK
        for c in s.calls[1:]:
            seen = c["cache_write"] + c["cache_read"]
            if c["cache_write"] >= th["cache_break"] and seen and c["cache_write"] / seen > 0.5:
                tells.append({"code": "CACHE_BREAK", "session": s.sid,
                              "what": "call %d rebuilt %d tokens of cache" % (c["idx"] + 1, c["cache_write"]),
                              "count": 1, "lines": [c["line"]], "tokens": c["cache_write"],
                              "lever": "Look at what changed just before this call: a long pause past the cache lifetime, a model switch, an edited instruction file, or a changed tool list. Keep the opening of the context stable during a session.",
                              "undo": "Nothing to undo; it is a habit."})
        # LONG_HAUL
        if s.calls:
            over = sum(max(s.context(c) - th["long_haul"], 0) for c in s.calls)
            peak = max(s.context(c) for c in s.calls)
            if peak >= th["long_haul"]:
                tells.append({"code": "LONG_HAUL", "session": s.sid,
                              "what": "peak context %d tokens over %d calls" % (peak, len(s.calls)),
                              "count": len(s.calls), "lines": [s.calls[-1]["line"]],
                              "tokens": over,
                              "lever": "Split the work: finish one goal, write a short handoff note, and start a fresh session for the next goal, or compact earlier.",
                              "undo": "Keep working in one session."})

    # RELEARN (across sessions)
    for key, pairs in fail_sessions.items():
        sids = sorted(set(sid for sid, _t in pairs))
        if len(sids) >= th["relearn"]:
            by_s = {s.sid: s for s in sessions}
            toks = 0
            for sid in sids:
                ts = [t for x, t in pairs if x == sid]
                toks += _direct(by_s[sid], ts)
            tells.append({"code": "RELEARN", "session": "*", "what": key,
                          "count": len(sids), "lines": [], "sessions": sids,
                          "tokens": toks,
                          "lever": "Every session rediscovers this. Find the form that works here and write one line about it in the project instruction file.",
                          "undo": "Delete that line."})

    # HEAVY_START (aggregate)
    with_calls = [s for s in sessions if s.calls]
    heavy = [s for s in with_calls if s.context(s.calls[0]) >= th["heavy_start"]]
    if with_calls and len(heavy) * 2 >= len(with_calls) and heavy:
        tells.append({"code": "HEAVY_START", "session": "*",
                      "what": "%d of %d sessions open with at least %d tokens of context" % (len(heavy), len(with_calls), th["heavy_start"]),
                      "count": len(heavy), "lines": [], "sessions": [s.sid for s in heavy],
                      "tokens": sum(s.context(s.calls[0]) for s in heavy),
                      "lever": "Measure what loads before the first word: instruction files, skill descriptions, tool and server definitions. Turn off what this project never uses.",
                      "undo": "Turn it back on."})

    # IDLE_TOOL
    if loaded:
        used = Counter()
        for s in sessions:
            for name in set(t["name"] for t in s.tools):
                used[name] += 1
        n = len(sessions)
        if n >= th["idle_sessions"]:
            for name, size in loaded.items():
                if used.get(name, 0) == 0:
                    tells.append({"code": "IDLE_TOOL", "session": "*", "what": name,
                                  "count": n, "lines": [],
                                  "tokens": (size or 0) * sum(len(s.calls) for s in sessions),
                                  "lever": "Loaded in every call, used in none of %d sessions. Leave it out for this project." % n,
                                  "undo": "Add it back to the tool or server list."})

    tells.sort(key=lambda t: (-t["tokens"], t["code"], str(t["what"])))
    return tells


def read_loaded(path):
    """Lines: 'tool_name' or 'tool_name<TAB or space>approx_tokens'."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            size = None
            if len(parts) > 1:
                try:
                    size = int(parts[1])
                except ValueError:
                    size = None
            out[parts[0]] = size
    return out


# ---------------------------------------------------------------- trial

def read_outcomes(path):
    out = {}
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            sid = (row.get("session") or "").strip()
            val = (row.get("passed") or "").strip().lower()
            if not sid or val == "":
                continue
            out[sid] = val in ("1", "true", "yes", "y", "pass", "passed")
    return out


def session_metric(s, card):
    if card:
        total = 0.0
        for c in s.calls:
            v = call_cost(card, c)
            if v is None:
                return None
            total += v
        return total
    return sum(s.total(c) for c in s.calls)


def trial(before, after, card=None, outcomes=None, min_n=5, min_saving=0.10, tolerance=0.0):
    unit = "usd" if card else "tokens"
    b = [session_metric(s, card) for s in before]
    a = [session_metric(s, card) for s in after]
    if None in a or None in b:
        raise SystemExit("token-tab: some models in these sessions have no price; add them to the price card or drop --prices")
    res = {"unit": unit, "before_n": len(b), "after_n": len(a)}
    if len(b) < min_n or len(a) < min_n:
        res.update({"verdict": "NOT_ENOUGH",
                    "why": "need at least %d sessions on each side (have %d before, %d after)" % (min_n, len(b), len(a))})
        return res
    mb, ma = statistics.median(b), statistics.median(a)
    change = (ma - mb) / mb if mb else 0.0
    below = sum(1 for x in a if x < mb) / len(a)
    res.update({"before_median": round(mb, 4), "after_median": round(ma, 4),
                "change": round(change, 3), "after_below_before_median": round(below, 3)})
    rate_b = rate_a = None
    if outcomes is not None:
        ob = [outcomes[s.sid] for s in before if s.sid in outcomes]
        oa = [outcomes[s.sid] for s in after if s.sid in outcomes]
        if len(ob) < min_n or len(oa) < min_n:
            res.update({"verdict": "NOT_ENOUGH",
                        "why": "outcomes recorded for %d before and %d after; need %d each" % (len(ob), len(oa), min_n)})
            return res
        rate_b, rate_a = sum(ob) / len(ob), sum(oa) / len(oa)
        res.update({"before_pass_rate": round(rate_b, 3), "after_pass_rate": round(rate_a, 3)})
        if rate_a < rate_b - tolerance - 1e-9:
            res.update({"verdict": "REVERT", "why": "fewer tasks passed after the change"})
            return res
    if -change >= min_saving and below >= 0.5:
        if rate_a is None:
            res.update({"verdict": "KEEP_UNVERIFIED",
                        "why": "cheaper, but no outcomes were recorded, so quality is unchecked"})
        else:
            res.update({"verdict": "KEEP", "why": "cheaper and tasks passed at least as often"})
    else:
        res.update({"verdict": "NO_GAIN",
                    "why": "median change %.1f%% does not clear the %.0f%% bar" % (change * 100, min_saving * 100)})
    return res


TRIAL_EXIT = {"KEEP": 0, "KEEP_UNVERIFIED": 1, "NO_GAIN": 1, "REVERT": 2, "NOT_ENOUGH": 3}


# ---------------------------------------------------------------- output

def fmt_n(n):
    return "{:,}".format(int(n))


def render_bill(b):
    out = ["# Token tab", ""]
    t = b["totals"]
    line = "%d session%s, %s calls, %s tokens" % (t["sessions"], "" if t["sessions"] == 1 else "s", fmt_n(t["calls"]), fmt_n(t["tokens"]))
    if t["cost"] is not None:
        line += ", $%.2f at prices checked %s" % (t["cost"], b["prices_as_of"])
    out += [line, ""]
    if b["unpriced_models"]:
        out += ["Unpriced models (cost left blank): " + ", ".join(b["unpriced_models"]), ""]
    out += ["| item | tokens | share |", "|---|---:|---:|"]
    for it in b["items"]:
        out.append("| %s | %s | %.1f%% |" % (it["item"], fmt_n(it["tokens"]), it["share"] * 100))
    out += ["", "Opening context across sessions: %s tokens. Tool output fed back (estimate): %s tokens." % (
        fmt_n(t["opening_context_tokens"]), fmt_n(t["tool_result_tokens_est"])), ""]
    out += ["| session | calls | tokens | cost | opening | peak |", "|---|---:|---:|---:|---:|---:|"]
    for r in b["sessions"]:
        cost = "" if r["cost"] is None else "$%.4f" % r["cost"]
        out.append("| %s | %d | %s | %s | %s | %s |" % (r["session"], r["calls"], fmt_n(r["total"]), cost,
                                                     fmt_n(r["start_context"]), fmt_n(r["peak_context"])))
    return "\n".join(out) + "\n"


def render_tells(tells, limit):
    if not tells:
        return "# Waste tells\n\nNo tells at these thresholds.\n"
    out = ["# Waste tells", "", "Tokens are estimates of what each tell touched, not a promise of savings.", ""]
    for i, t in enumerate(tells[:limit], 1):
        where = t["session"] if t["session"] != "*" else "%d sessions" % len(t.get("sessions", [])) if t.get("sessions") else "all sessions"
        out.append("%d. **%s** in %s: `%s` (x%d, ~%s tokens)" % (i, t["code"], where, t["what"], t["count"], fmt_n(t["tokens"])))
        if t["lines"]:
            out.append("   lines: " + ", ".join(str(x) for x in t["lines"][:8]))
        out.append("   lever: " + t["lever"])
        out.append("   undo: " + t["undo"])
    if len(tells) > limit:
        out.append("")
        out.append("%d more not shown (use --limit)." % (len(tells) - limit))
    return "\n".join(out) + "\n"


def render_trial(r, change_text):
    out = ["# Trial: %s" % (change_text or "(change not described)"), "", "Verdict: **%s**, %s." % (r["verdict"], r["why"]), ""]
    out.append("Sessions: %d before, %d after. Unit: %s." % (r["before_n"], r["after_n"], r["unit"]))
    if "before_median" in r:
        out.append("Median per session: %s before, %s after (%+.1f%%). %.0f%% of after-sessions came in under the old median." % (
            r["before_median"], r["after_median"], r["change"] * 100, r["after_below_before_median"] * 100))
    if "before_pass_rate" in r:
        out.append("Tasks passed: %.0f%% before, %.0f%% after." % (r["before_pass_rate"] * 100, r["after_pass_rate"] * 100))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- demo

def _cc_lines(sid, steps):
    """Build Claude Code style lines. steps: list of (usage, tool_name, tool_input, ok, result_text)."""
    lines = []
    for i, (usage, name, inp, ok, result) in enumerate(steps):
        mid = "%s-m%d" % (sid, i)
        content = [{"type": "text", "text": "working"}]
        if name:
            content.append({"type": "tool_use", "id": mid + "-t", "name": name, "input": inp})
        lines.append({"type": "assistant", "sessionId": sid, "message": {
            "id": mid, "model": "example-model", "content": content,
            "usage": {"input_tokens": usage[0], "cache_creation_input_tokens": usage[1],
                      "cache_read_input_tokens": usage[2], "output_tokens": usage[3]}}})
        if name:
            lines.append({"type": "user", "sessionId": sid, "message": {"content": [
                {"type": "tool_result", "tool_use_id": mid + "-t", "is_error": not ok, "content": result}]}})
    return [json.dumps(x) for x in lines]


def demo(folder):
    os.makedirs(os.path.join(folder, "sessions"), exist_ok=True)
    os.makedirs(os.path.join(folder, "before"), exist_ok=True)
    os.makedirs(os.path.join(folder, "after"), exist_ok=True)
    fail = "bash: pytest: command not found"
    big = "x" * 40000
    for k in range(4):
        sid = "demo-%d" % (k + 1)
        steps = [((50, 24000, 0, 300), "Bash", {"command": "pytest -q"}, False, fail),
                 ((40, 300, 24000, 200), "Bash", {"command": "python -m pytest -q"}, True, "12 passed")]
        if k == 0:
            steps += [((30, 200, 24300, 150), "Bash", {"command": "make build"}, False, "error: missing step")] * 3
        if k == 1:
            steps += [((30, 100, 24600, 120), "Bash", {"command": "cat logs/app.log"}, True, big),
                      ((30, 22000, 3000, 100), None, None, True, ""),
                      ((30, 200, 34600, 100), None, None, True, "")]
        if k == 2:
            steps += [((20, 100, 24400, 80), "Read", {"file_path": "src/app.py"}, True, "y" * 6000)] * 3
        with open(os.path.join(folder, "sessions", sid + ".jsonl"), "w") as fh:
            fh.write("\n".join(_cc_lines(sid, steps)) + "\n")
    with open(os.path.join(folder, "loaded_tools.txt"), "w") as fh:
        fh.write("# tool name and rough tokens its definition adds to every call\nBash 900\nRead 600\nNotebookEdit 1200\nWebFetch 800\n")
    rows = ["session,passed"]
    for side, opening, n in (("before", 26000, 6), ("after", 15000, 6)):
        for k in range(n):
            sid = "%s-%d" % (side, k + 1)
            steps = [((50, opening + k * 300, 0, 400), "Bash", {"command": "make test"}, True, "ok")]
            steps += [((40, 300, opening + k * 300, 300), None, None, True, "")] * 4
            with open(os.path.join(folder, side, sid + ".jsonl"), "w") as fh:
                fh.write("\n".join(_cc_lines(sid, steps)) + "\n")
            rows.append("%s,%d" % (sid, 0 if (side == "before" and k == 5) else 1))
    with open(os.path.join(folder, "outcomes.csv"), "w") as fh:
        fh.write("\n".join(rows) + "\n")
    with open(os.path.join(folder, "prices.example.json"), "w") as fh:
        json.dump({"as_of": "EXAMPLE-ONLY",
                   "source": "Made-up numbers for the demo. Copy real per-million-token prices from your provider's pricing page.",
                   "models": {"example-model": {"input": 3.0, "cache_write": 3.75, "cache_read": 0.3, "output": 15.0}}},
                  fh, indent=2)
    with open(os.path.join(folder, "README.txt"), "w") as fh:
        fh.write("SYNTHETIC DEMO DATA written by token-tab demo. Not real sessions or real prices.\n")
    return folder


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(prog="tab.py", description="Itemize and trim what a coding agent spends.")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("bill", help="itemized tab")
    p.add_argument("paths", nargs="+")
    p.add_argument("--prices")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("tells", help="ranked waste tells")
    p.add_argument("paths", nargs="+")
    p.add_argument("--loaded", help="file listing loaded tools (name [tokens])")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--json", action="store_true")
    for k, v in DEFAULTS.items():
        p.add_argument("--" + k.replace("_", "-"), type=int, default=v)

    p = sub.add_parser("trial", help="before/after verdict for one change")
    p.add_argument("--before", nargs="+", required=True)
    p.add_argument("--after", nargs="+", required=True)
    p.add_argument("--prices")
    p.add_argument("--outcomes")
    p.add_argument("--change", default="")
    p.add_argument("--min-n", type=int, default=5)
    p.add_argument("--min-saving", type=float, default=0.10)
    p.add_argument("--tolerance", type=float, default=0.0)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("demo", help="write synthetic sample data")
    p.add_argument("folder", nargs="?", default="token-tab-demo")

    a = ap.parse_args(argv)
    if a.cmd == "demo":
        f = demo(a.folder)
        print("Synthetic demo written to %s (not real data)." % f)
        return 0
    if a.cmd == "bill":
        b = bill(load_paths(a.paths), load_prices(a.prices))
        print(json.dumps(b, indent=1) if a.json else render_bill(b), end="" if not a.json else "\n")
        return 0
    if a.cmd == "tells":
        th = {k: getattr(a, k) for k in DEFAULTS}
        loaded = read_loaded(a.loaded) if a.loaded else None
        tells = find_tells(load_paths(a.paths), th, loaded)
        print(json.dumps(tells, indent=1) if a.json else render_tells(tells, a.limit), end="" if not a.json else "\n")
        return 1 if tells else 0
    if a.cmd == "trial":
        card = load_prices(a.prices)
        outcomes = read_outcomes(a.outcomes) if a.outcomes else None
        r = trial(load_paths(a.before), load_paths(a.after), card, outcomes, a.min_n, a.min_saving, a.tolerance)
        r["change_text"] = a.change
        print(json.dumps(r, indent=1) if a.json else render_trial(r, a.change), end="" if not a.json else "\n")
        return TRIAL_EXIT[r["verdict"]]
    return 2


if __name__ == "__main__":
    sys.exit(main())
