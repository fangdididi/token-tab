# token-tab

**English** | [简体中文](README.zh-CN.md)

> Find out where your coding agent's tokens go, fix the waste that repeats, and prove the fix worked.

token-tab is an agent skill plus a small offline command line tool. It reads the session logs your coding agent already writes, turns them into an itemized bill, points at repeat waste with line-level evidence, and then judges one change with a before/after trial that also checks the agent still gets tasks done.

It is read-only. It never edits your agent settings, never calls a model, and never sends your logs anywhere.

## Why

Most of an agent bill is paid quietly on every call: instructions, tool definitions and the whole conversation are sent again and again. You rarely see the patterns that make it worse:

- a command that fails in every new session until the agent finds the one that works
- a giant log pasted into context once and then carried on every later call
- a file read four times in one session
- the cache rebuilt mid-session because something at the top of the context changed
- tools and servers loaded in every call but never used in this project

Generic "save tokens" advice does not tell you which of these is yours, and a tweak that looks cheaper can quietly make the agent fail more. token-tab answers both questions from your own records.

## Features

- **Itemized bill** per session and in total: cached re-reads, cache writes, fresh input, output, opening context, peak context, and estimated tool output fed back.
- **Money only with dated prices.** You supply a price card with an `as_of` date from your provider's pricing page; unknown models leave the cost blank instead of guessing.
- **Eight waste tells**: `LOOP`, `RELEARN`, `FLOOD`, `REREAD`, `CACHE_BREAK`, `LONG_HAUL`, `HEAVY_START`, `IDLE_TOOL`, each with evidence lines, a token estimate, a lever and an undo.
- **One-change trial** with five verdicts: `KEEP`, `KEEP_UNVERIFIED`, `NO_GAIN`, `REVERT`, `NOT_ENOUGH`. A drop in pass rate always means `REVERT`.
- **Reads Claude Code transcripts directly** (repeated lines for one response are counted once) and a simple generic JSONL format for any other agent.
- **Standard library Python**, no install, 77 unit tests, a synthetic demo to try it in seconds.

## Install

Clone the repo, then put the folder where your agent looks for skills.

**Claude Code**

```bash
git clone https://github.com/fangdididi/token-tab.git
mkdir -p ~/.claude/skills && cp -r token-tab ~/.claude/skills/token-tab
```

**Codex**

```bash
mkdir -p ~/.codex/skills && cp -r token-tab ~/.codex/skills/token-tab
```

**Cursor and other agents**

Copy the folder into the project (for example `.cursor/skills/token-tab/` or `skills/token-tab/`) and point your rules or `AGENTS.md` at `token-tab/SKILL.md`. Any agent that can read a Markdown file and run Python can follow it.

The script also works on its own: `python3 scripts/tab.py --help`.

## Usage

Ask your agent in plain words:

- "Use token-tab on my last two weeks of sessions in this repo."
- "Why are my sessions so expensive? Show me the tab and the top tells."
- "I removed two MCP servers on Monday. Run a token-tab trial."

Or run the tool directly:

```bash
# try it on synthetic data first
python3 scripts/tab.py demo /tmp/tt-demo

# 1. the bill
python3 scripts/tab.py bill ~/.claude/projects/<project-folder>/ --prices prices.json

# 2. the tells (exit code 1 when any are found)
python3 scripts/tab.py tells ~/.claude/projects/<project-folder>/ --loaded loaded-tools.txt

# 3. the trial for one change
python3 scripts/tab.py trial --before before/ --after after/ \
  --outcomes outcomes.csv --prices prices.json --change "drop unused servers"
```

Add `--json` to any command for machine-readable output. Tell thresholds can be tuned, for example `--loop 2 --flood 4000 --long-haul 80000`.

### Other agents: the generic format

One JSON object per line. A `tool` line belongs to the latest `call` line of the same session.

```json
{"kind":"call","session":"s1","model":"your-model","in":120,"cache_write":0,"cache_read":24000,"out":310}
{"kind":"tool","session":"s1","name":"Bash","args":{"command":"npm test"},"ok":false,"result_tokens":850}
```

## Example

From the bundled synthetic demo (not real data):

```text
$ python3 scripts/tab.py tells /tmp/tt-demo/sessions --loaded /tmp/tt-demo/loaded_tools.txt --idle-sessions 4
1. **HEAVY_START** in 4 sessions: `4 of 4 sessions open with at least 20000 tokens of context` (x4, ~96,200 tokens)
2. **CACHE_BREAK** in demo-2: `call 4 rebuilt 22000 tokens of cache` (x1, ~22,000 tokens)
3. **IDLE_TOOL** in all sessions: `NotebookEdit` (x4, ~20,400 tokens)
4. **FLOOD** in demo-2: `bash:cat logs/app.log` (x1, ~20,000 tokens)
5. **IDLE_TOOL** in all sessions: `WebFetch` (x4, ~13,600 tokens)
6. **REREAD** in demo-3: `src/app.py` (x3, ~3,000 tokens)
7. **RELEARN** in 4 sessions: `bash:pytest -q` (x4, ~1,232 tokens)
8. **LOOP** in demo-1: `bash:make build` (x3, ~310 tokens)

$ python3 scripts/tab.py trial --before /tmp/tt-demo/before --after /tmp/tt-demo/after --outcomes /tmp/tt-demo/outcomes.csv --change "trim tool list"
Verdict: **KEEP**, cheaper and tasks passed at least as often.
Sessions: 6 before, 6 after. Unit: tokens.
Median per session: 136760.0 before, 81760.0 after (-40.2%). 100% of after-sessions came in under the old median.
Tasks passed: 83% before, 100% after.
```

## How a run goes

1. Write a short tab card: agent, project, window, what "passed" means, privacy.
2. Copy one project's logs into a folder.
3. Read the bill.
4. Read the tells and verify each one you plan to act on against its evidence lines.
5. Pick one change, write a change card with an undo, and get a yes.
6. Run the trial on at least five sessions per side.
7. Report the verdict honestly, including `NO_GAIN`.

## File structure

```text
token-tab/
├── SKILL.md                 # skill instructions (English)
├── SKILL.zh-CN.md           # skill instructions (Chinese)
├── README.md
├── README.zh-CN.md
├── LICENSE
├── scripts/
│   └── tab.py               # bill, tells, trial, demo
├── examples/
│   ├── prices.template.json
│   ├── outcomes.template.csv
│   ├── generic-session.jsonl
│   └── loaded-tools.txt
└── tests/
    └── test_tab.py          # python3 -m unittest discover -s tests
```

## Privacy

Session logs can contain source code, file paths and secrets. token-tab only reads local files and prints keys, counts and line numbers. Keep reports free of raw log content.

## License

MIT, see [LICENSE](LICENSE).
