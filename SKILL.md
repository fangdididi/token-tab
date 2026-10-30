---
name: token-tab
description: Read a coding agent's own session logs, turn them into an itemized bill, point at the waste that repeats (failing loops, facts relearned every session, floods of tool output, rereads, cache rebuilds, heavy openings, tools that are loaded but never used), then change one thing and prove with a before/after trial that it saved money without hurting results. Use when someone asks why their agent costs so much, wants to cut token use, or wants to know whether a tweak actually helped.
---

# token-tab

**Language:** English | [简体中文](SKILL.zh-CN.md)

## What this is

Coding agents such as Claude Code, Codex, Cursor or the Copilot CLI bill by the token, and most of the bill is invisible while you work. Every call pays again for the instructions, every loaded tool definition and the whole conversation so far, mostly as cheap cached re-reads, sometimes as expensive cache rebuilds. The waste that matters is the waste that repeats: the agent rediscovers the same broken command in every session, pastes a 40 KB log into context and then carries it on every later call, or re-reads one file four times.

token-tab is a careful way to look at that. It works only from records the user already has (the agent's session logs), it never changes anything on its own, and it ends with a verdict on one change instead of a list of tips.

The flow has four parts:

1. **The tab**: an itemized bill per session and in total, with cost only when the user supplies dated prices.
2. **The tells**: ranked, evidence-linked signs of repeat waste, each with a lever and a way to undo it.
3. **One change**: the single lever with the best ratio of verified waste to effort and risk.
4. **The trial**: the same kind of work before and after the change, with a pass/fail outcome per session, and a verdict of KEEP, KEEP_UNVERIFIED, NO_GAIN, REVERT or NOT_ENOUGH.

## When to use it

- "Why is my agent bill so high this month?"
- "Which of my skills, MCP servers or instruction files are costing me tokens?"
- "My sessions keep getting slow and expensive near the end."
- "I trimmed my tool list. Did it actually help?"
- Before rolling a cost-saving setting out to a whole team.

Do not use it to judge a single session in isolation, to estimate costs for an agent that has no logs yet, or to compare two different models on quality. It measures spend on the user's own repeated work.

## What you need

- Session logs from one agent on one project. Claude Code keeps JSONL transcripts under `~/.claude/projects/<project-folder>/`. Other agents keep logs in their own places; if the format is different, convert each session to the generic format below.
- Optional: a price card (`examples/prices.template.json`) filled in from the provider's own pricing page, with the date you checked it in `as_of`.
- Optional: a list of tools loaded in each session, with a rough token size for each definition, to spot tools that are never used.
- For the trial: an outcomes file saying whether each session's task passed.

Generic format, one JSON object per line:

```json
{"kind":"call","session":"s1","model":"your-model","in":120,"cache_write":0,"cache_read":24000,"out":310}
{"kind":"tool","session":"s1","name":"Bash","args":{"command":"npm test"},"ok":false,"result_tokens":850}
```

A `tool` line belongs to the most recent `call` line in the same session.

## Steps

### 1. Write the tab card

Before reading any numbers, agree on the scope and write it down in a few lines:

- agent and version, project, date window
- what a "task passed" means here (tests green, PR merged, user accepted)
- whether cost is needed in money or tokens are enough
- what must stay private: logs contain code, file paths and sometimes secrets, so work on a local copy, quote only keys and line numbers in reports, and never paste raw log content into a chat or issue

### 2. Collect a clean set

Copy the logs for that project and window into one folder. Keep one agent per folder. Drop sessions that were experiments with other settings, or label them and keep them in their own folder. Never edit or delete the originals.

### 3. Read the tab

```bash
python3 scripts/tab.py bill sessions/ --prices prices.json
```

Look at three things first:

- **The item split.** Cached re-reads are usually most of the token count but cheap per token. A big share of cache *writes* means the cache keeps getting rebuilt.
- **Opening context.** What a session costs before the first word: instructions, skill descriptions, tool and server definitions. It is paid in every session.
- **The top sessions.** Open the two or three biggest and see what they were doing.

If `unpriced_models` is not empty, the money column is blank on purpose. Add the missing prices or report tokens only. Never guess a price.

### 4. Read the tells

```bash
python3 scripts/tab.py tells sessions/ --loaded loaded_tools.txt
```

| Tell | What it means | Usual lever |
|---|---|---|
| `LOOP` | the same action failed three or more times in one session | a stop-and-rethink rule after the second identical failure |
| `RELEARN` | the same action failed in three or more sessions | one line in the project instruction file with the form that works |
| `FLOOD` | one tool result was huge and rode along on every later call | filter, head, grep, ranges, quieter flags |
| `REREAD` | the same file was read three or more times in one session | read once, keep a note, reread after a change only |
| `CACHE_BREAK` | a later call rebuilt most of the cache | keep the start of the context stable; look at pauses, model switches, edited instruction files |
| `LONG_HAUL` | the context grew past the long-session threshold | finish one goal, hand off, start fresh |
| `HEAVY_START` | most sessions open with a big context | turn off instructions, skills, tools or servers this project never uses |
| `IDLE_TOOL` | a loaded tool was not called in any session | leave it out for this project |

Token numbers on tells are estimates of what each tell touched (tool output is estimated at four characters per token). They are a ranking aid, not a savings promise.

Then **verify every tell you plan to act on** by opening the evidence lines. Dismiss it, with a reason, when:

- the retries were deliberate (a flaky network call, a server that needed time)
- the file changed between reads
- the big output was truly needed
- the cache rebuild came from a pause the user wanted

Thresholds can be changed per run (`--loop 2`, `--flood 4000` and so on) when the project's normal looks different.

### 5. Pick one change

Choose one lever. Prefer, in order: the largest verified tell, the smallest edit, the easiest undo. Write a change card:

- the change, as an exact diff or setting
- the tell it targets and the evidence
- the expected effect, in plain words
- how to undo it
- the guard: what must not get worse (task pass rate)

Show the card to the user and get a yes before touching any instruction file, settings file or tool list. Never change two things at once; the trial cannot separate them.

### 6. Run the trial

Collect at least five sessions of comparable work from before the change and five from after. Record outcomes:

```csv
session,passed
before-01,1
after-01,1
```

```bash
python3 scripts/tab.py trial --before before/ --after after/ \
  --outcomes outcomes.csv --prices prices.json --change "drop unused WebFetch tool"
```

| Verdict | Meaning | Exit code |
|---|---|---|
| `KEEP` | the median session got at least 10% cheaper, most after-sessions came in under the old median, and tasks passed at least as often | 0 |
| `KEEP_UNVERIFIED` | cheaper, but no outcomes were recorded | 1 |
| `NO_GAIN` | the drop did not clear the bar | 1 |
| `REVERT` | fewer tasks passed after the change | 2 |
| `NOT_ENOUGH` | too few sessions or outcomes on one side | 3 |

A `REVERT` wins over any saving. A cheaper agent that fails more is not cheaper.

### 7. Report

Keep the report short:

1. The tab in two lines: total, biggest item, opening context.
2. Tells acted on, tells dismissed and why.
3. The change card.
4. The verdict, with the medians and pass rates.
5. The next candidate lever, if any.

## Examples

The numbers below are illustrative, to show how the steps fit together.

**Relearned command.** In 9 of 12 sessions the agent first ran `pytest`, got "command not found", then found `uv run pytest`. The tell is `RELEARN` with nine sessions. The change is one line in the project's instruction file: "Run tests with `uv run pytest -q`." Trial: median session tokens fell 6%, pass rate unchanged, verdict `NO_GAIN` at the default 10% bar. The line is still worth keeping because it is free and harmless, and the report says so honestly instead of claiming a saving.

**Flooded log.** One session ran `cat logs/app.log` and pulled about 20,000 tokens of output, then made 30 more calls with it in context. The tell is `FLOOD`. The lever is an instruction to use `tail -n 200` or `grep` on logs. One session is not a trend, so the report checks other sessions for the same pattern before proposing a rule.

**Heavy openings.** Every session opens with about 26,000 tokens. The tool list shows two servers that were never used in 14 sessions. After turning them off for this project, ten after-sessions open at about 15,000 tokens, median cost down 31%, pass rate 9/10 before and 10/10 after. Verdict `KEEP`.

## Tips

- Compare like with like: the same project, similar tasks, the same model. A trial across a model switch measures the model, not the change.
- Prices change. Keep `as_of` honest and re-check it before reporting money.
- A tell with a big token number but no verified evidence is not a finding.
- Keep the change card and the verdict next to the project, so the next person knows why a setting is the way it is.
- If the user wants a hard spend cap or automatic loop stopping, that is a job for the agent's own settings or hooks; this skill measures and recommends, it does not enforce.

## Files

- `scripts/tab.py`: the command line tool (`bill`, `tells`, `trial`, `demo`), standard library only.
- `examples/`: price card template, outcomes template, generic format sample, loaded tools sample.
- `tests/`: unit tests (`python3 -m unittest discover -s tests`).
