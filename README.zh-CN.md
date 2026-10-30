# token-tab

[English](README.md) | **简体中文**

> 弄清编码智能体的 token 花在了哪里，修掉反复出现的浪费，并证明修得有效。

token-tab 是一个智能体技能，外加一个离线运行的小命令行工具。它读取编码智能体本来就会写下的会话日志，整理成逐项账单，带着精确到日志行的证据指出重复浪费，然后用改动前后的对照试验评判“一处改动”，同时检查智能体是否还能把任务做成。

它是只读的：从不修改你的智能体设置，从不调用模型，也从不把日志发到任何地方。

## 为什么需要它

智能体账单的大头是在每次调用时悄悄付掉的：指令、工具定义、整段对话，一遍又一遍地发送。让账单变得更糟的模式，你通常看不到：

- 某条命令在每个新会话里都会失败，直到智能体找到能用的那条
- 一份巨大的日志被贴进上下文一次，之后每次调用都拖着它
- 同一个文件在一个会话里被读了四遍
- 因为上下文开头变了，缓存在会话中途被整块重建
- 某些工具和服务器每次调用都加载，但在这个项目里从没用过

泛泛的“省 token”建议说不清哪一条是你的问题；而一个看起来更省的调整，可能会悄悄让智能体失败得更多。token-tab 用你自己的记录回答这两个问题。

## 功能

- **逐项账单**：按会话和总计列出缓存重读、缓存写入、新鲜输入、输出、开场上下文、峰值上下文，以及回灌给模型的工具输出估算。
- **有带日期的价格才折算成钱。** 你从服务商价格页抄价格到价格卡里并填写 `as_of` 日期；遇到没有价格的模型，金额留空，不猜。
- **八种浪费迹象**：`LOOP`、`RELEARN`、`FLOOD`、`REREAD`、`CACHE_BREAK`、`LONG_HAUL`、`HEAVY_START`、`IDLE_TOOL`，每条都带证据行、token 估算、杠杆和撤销方法。
- **单一改动试验**，五种结论：`KEEP`、`KEEP_UNVERIFIED`、`NO_GAIN`、`REVERT`、`NOT_ENOUGH`。通过率下降一律判 `REVERT`。
- **直接读取 Claude Code 会话记录**（同一次响应被记成多行时只计一次），其他智能体可使用一个简单的通用 JSONL 格式。
- **纯标准库 Python**，免安装，77 个单元测试，附带合成演示数据，几秒钟就能试用。

## 安装

克隆仓库，然后把文件夹放到智能体查找技能的位置。

**Claude Code**

```bash
git clone https://github.com/fangdididi/token-tab.git
mkdir -p ~/.claude/skills && cp -r token-tab ~/.claude/skills/token-tab
```

**Codex**

```bash
mkdir -p ~/.codex/skills && cp -r token-tab ~/.codex/skills/token-tab
```

**Cursor 及其他智能体**

把文件夹复制进项目（例如 `.cursor/skills/token-tab/` 或 `skills/token-tab/`），并在规则文件或 `AGENTS.md` 里指向 `token-tab/SKILL.md`。任何能读 Markdown 并运行 Python 的智能体都能照着做。

脚本也可以单独使用：`python3 scripts/tab.py --help`。

## 用法

直接用大白话告诉智能体：

- “用 token-tab 看一下我这个仓库最近两周的会话。”
- “我的会话为什么这么贵？给我看账单和排名靠前的迹象。”
- “我周一去掉了两个 MCP 服务器，跑一次 token-tab 试验。”

也可以直接运行工具：

```bash
# 先用合成数据试一下
python3 scripts/tab.py demo /tmp/tt-demo

# 1. 账单
python3 scripts/tab.py bill ~/.claude/projects/<项目目录>/ --prices prices.json

# 2. 迹象（发现任何迹象时退出码为 1）
python3 scripts/tab.py tells ~/.claude/projects/<项目目录>/ --loaded loaded-tools.txt

# 3. 针对一处改动的试验
python3 scripts/tab.py trial --before before/ --after after/ \
  --outcomes outcomes.csv --prices prices.json --change "去掉未使用的服务器"
```

任何命令加 `--json` 即可输出机器可读结果。迹象阈值可调，例如 `--loop 2 --flood 4000 --long-haul 80000`。

### 其他智能体：通用格式

每行一个 JSON 对象。`tool` 行归属于同一会话里最近的一条 `call` 行。

```json
{"kind":"call","session":"s1","model":"your-model","in":120,"cache_write":0,"cache_read":24000,"out":310}
{"kind":"tool","session":"s1","name":"Bash","args":{"command":"npm test"},"ok":false,"result_tokens":850}
```

## 示例

来自内置的合成演示（不是真实数据）：

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

## 一次完整流程

1. 写一张简短的账单卡：智能体、项目、时间范围、“通过”的定义、隐私要求。
2. 把一个项目的日志复制到一个文件夹。
3. 读账单。
4. 读迹象；凡是打算处理的，都对照证据行核实。
5. 只挑一处改动，写好带撤销方法的改动卡，并征得同意。
6. 每侧至少五个会话，做对照试验。
7. 如实报告结论，包括 `NO_GAIN`。

## 文件结构

```text
token-tab/
├── SKILL.md                 # 技能说明（英文）
├── SKILL.zh-CN.md           # 技能说明（中文）
├── README.md
├── README.zh-CN.md
├── LICENSE
├── scripts/
│   └── tab.py               # bill、tells、trial、demo
├── examples/
│   ├── prices.template.json
│   ├── outcomes.template.csv
│   ├── generic-session.jsonl
│   └── loaded-tools.txt
└── tests/
    └── test_tab.py          # python3 -m unittest discover -s tests
```

## 隐私

会话日志可能包含源代码、文件路径和密钥。token-tab 只读取本地文件，只输出键名、计数和行号。报告里不要出现原始日志内容。

## 许可证

MIT，见 [LICENSE](LICENSE)。
