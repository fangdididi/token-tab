import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import tab  # noqa: E402


def call(session="s", model="m", fresh=10, cw=0, cr=0, out=5):
    return json.dumps({"kind": "call", "session": session, "model": model, "in": fresh,
                       "cache_write": cw, "cache_read": cr, "out": out})


def tool(session="s", name="Bash", args=None, ok=True, result_tokens=10):
    return json.dumps({"kind": "tool", "session": session, "name": name,
                       "args": args if args is not None else {"command": "ls"},
                       "ok": ok, "result_tokens": result_tokens})


def parse(lines, sid="f"):
    return list(tab.parse_lines(lines, "mem", sid).values())


def cc_assistant(sid, mid, usage, tools=(), model="m"):
    content = [{"type": "tool_use", "id": tid, "name": n, "input": i} for tid, n, i in tools]
    return json.dumps({"type": "assistant", "sessionId": sid, "message": {
        "id": mid, "model": model, "content": content,
        "usage": {"input_tokens": usage[0], "cache_creation_input_tokens": usage[1],
                  "cache_read_input_tokens": usage[2], "output_tokens": usage[3]}}})


def cc_result(sid, tid, content, err=False):
    return json.dumps({"type": "user", "sessionId": sid, "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": err, "content": content}]}})


PRICES = {"as_of": "2026-01-01", "models": {"m": {"input": 1.0, "cache_write": 2.0, "cache_read": 0.1, "output": 10.0}}}


class EstimateTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(tab.est_tokens(""), 0)

    def test_rounds_up(self):
        self.assertEqual(tab.est_tokens("abcde"), 2)

    def test_flatten_list(self):
        self.assertEqual(tab.flatten_result([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]), "a\nb")

    def test_flatten_image(self):
        self.assertIn("[image]", tab.flatten_result([{"type": "image"}]))

    def test_flatten_none(self):
        self.assertEqual(tab.flatten_result(None), "")


class KeyTests(unittest.TestCase):
    def test_command_whitespace(self):
        self.assertEqual(tab.action_key("Bash", {"command": "ls   -la"}), tab.action_key("bash", {"command": "ls -la"}))

    def test_command_list(self):
        self.assertEqual(tab.action_key("shell", {"command": ["git", "status"]}), "shell:git status")

    def test_path(self):
        self.assertEqual(tab.action_key("Read", {"file_path": "a.py"}), "read:a.py")

    def test_other_args(self):
        self.assertTrue(tab.action_key("X", {"q": 1}).startswith("x:{"))

    def test_string_args(self):
        self.assertEqual(tab.action_key("X", "a  b"), "x:a b")

    def test_file_of(self):
        self.assertEqual(tab.file_of({"path": "b.txt"}), "b.txt")
        self.assertIsNone(tab.file_of({"q": 1}))


class GenericParseTests(unittest.TestCase):
    def test_call_fields(self):
        s = parse([call(fresh=1, cw=2, cr=3, out=4)])[0]
        c = s.calls[0]
        self.assertEqual((c["fresh"], c["cache_write"], c["cache_read"], c["out"]), (1, 2, 3, 4))

    def test_tool_attached_to_last_call(self):
        s = parse([call(), call(), tool()])[0]
        self.assertEqual(s.tools[0]["call_idx"], 1)

    def test_result_text_estimated(self):
        line = json.dumps({"kind": "tool", "name": "Bash", "result": "x" * 40})
        s = parse([line])[0]
        self.assertEqual(s.tools[0]["result_tokens"], 10)

    def test_bad_lines_counted(self):
        s = parse(["not json", "[1]", call()])
        self.assertEqual(s[0].skipped, 2)

    def test_sessions_split(self):
        ss = parse([call(session="a"), call(session="b")])
        self.assertEqual(sorted(x.sid for x in ss), ["a", "b"])

    def test_default_session(self):
        line = json.dumps({"kind": "call", "in": 1})
        self.assertEqual(parse([line], sid="file1")[0].sid, "file1")


class ClaudeParseTests(unittest.TestCase):
    def test_usage(self):
        s = parse([cc_assistant("x", "m1", (1, 2, 3, 4))])[0]
        self.assertEqual(s.calls[0]["cache_read"], 3)

    def test_duplicate_message_counted_once(self):
        a = cc_assistant("x", "m1", (1, 2, 3, 4))
        s = parse([a, a])[0]
        self.assertEqual(len(s.calls), 1)

    def test_tool_result_links(self):
        s = parse([cc_assistant("x", "m1", (1, 0, 0, 1), [("t1", "Bash", {"command": "pytest"})]),
                   cc_result("x", "t1", "y" * 80, err=True)])[0]
        self.assertFalse(s.tools[0]["ok"])
        self.assertEqual(s.tools[0]["result_tokens"], 20)

    def test_unknown_result_ignored(self):
        s = parse([cc_assistant("x", "m1", (1, 0, 0, 1)), cc_result("x", "nope", "z")])[0]
        self.assertEqual(s.tools, [])

    def test_list_content_result(self):
        s = parse([cc_assistant("x", "m1", (1, 0, 0, 1), [("t1", "Read", {"file_path": "a"})]),
                   cc_result("x", "t1", [{"type": "text", "text": "abcd"}])])[0]
        self.assertEqual(s.tools[0]["result_tokens"], 1)

    def test_other_types_skipped(self):
        s = parse([json.dumps({"type": "summary", "summary": "x"}), cc_assistant("x", "m1", (1, 0, 0, 1))])
        self.assertEqual(sum(x.skipped for x in s), 1)


class PriceTests(unittest.TestCase):
    def write(self, obj):
        fd, p = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
        return p

    def test_needs_as_of(self):
        with self.assertRaises(SystemExit):
            tab.load_prices(self.write({"models": PRICES["models"]}))

    def test_needs_all_fields(self):
        with self.assertRaises(SystemExit):
            tab.load_prices(self.write({"as_of": "x", "models": {"m": {"input": 1}}}))

    def test_none_path(self):
        self.assertIsNone(tab.load_prices(None))

    def test_longest_prefix(self):
        card = {"models": {"m": {"input": 1}, "m-big": {"input": 2}}}
        self.assertEqual(tab.price_row(card, "m-big-2")["input"], 2)

    def test_no_match(self):
        self.assertIsNone(tab.price_row(PRICES, "zzz"))

    def test_call_cost(self):
        c = {"model": "m", "fresh": 1e6, "cache_write": 0, "cache_read": 1e6, "out": 0}
        self.assertAlmostEqual(tab.call_cost(PRICES, c), 1.1)


class BillTests(unittest.TestCase):
    def test_totals(self):
        b = tab.bill(parse([call(fresh=10, cw=20, cr=30, out=40)]))
        self.assertEqual(b["totals"]["tokens"], 100)

    def test_items_share(self):
        b = tab.bill(parse([call(fresh=0, cw=0, cr=75, out=25)]))
        shares = {i["item"]: i["share"] for i in b["items"]}
        self.assertEqual(shares["re-reading cached context"], 0.75)

    def test_cost(self):
        b = tab.bill(parse([call(fresh=1000000, cw=0, cr=0, out=0)]), PRICES)
        self.assertEqual(b["totals"]["cost"], 1.0)

    def test_unpriced_blanks_cost(self):
        b = tab.bill(parse([call(model="other")]), PRICES)
        self.assertIsNone(b["totals"]["cost"])
        self.assertEqual(b["unpriced_models"], ["other"])

    def test_opening_and_peak(self):
        b = tab.bill(parse([call(fresh=5, cw=100), call(cr=300)]))
        r = b["sessions"][0]
        self.assertEqual((r["start_context"], r["peak_context"]), (105, 310))

    def test_sorted_by_size(self):
        b = tab.bill(parse([call(session="small", fresh=1), call(session="big", fresh=999)]))
        self.assertEqual(b["sessions"][0]["session"], "big")

    def test_render(self):
        txt = tab.render_bill(tab.bill(parse([call()]), PRICES))
        self.assertIn("prices checked 2026-01-01", txt)


class TellTests(unittest.TestCase):
    def codes(self, lines, **kw):
        return [t["code"] for t in tab.find_tells(parse(lines), kw.pop("th", None), kw.pop("loaded", None))]

    def test_loop(self):
        bad = tool(args={"command": "make"}, ok=False)
        self.assertIn("LOOP", self.codes([call(), bad, call(), bad, call(), bad]))

    def test_no_loop_under_threshold(self):
        bad = tool(args={"command": "make"}, ok=False)
        self.assertNotIn("LOOP", self.codes([call(), bad, call(), bad]))

    def test_success_not_loop(self):
        ok = tool(args={"command": "make"}, ok=True)
        self.assertNotIn("LOOP", self.codes([call(), ok, call(), ok, call(), ok]))

    def test_relearn_across_sessions(self):
        lines = []
        for s in "abc":
            lines += [call(session=s), tool(session=s, args={"command": "pytest"}, ok=False)]
        tells = tab.find_tells(parse(lines))
        r = [t for t in tells if t["code"] == "RELEARN"][0]
        self.assertEqual(r["sessions"], ["a", "b", "c"])

    def test_relearn_needs_three(self):
        lines = []
        for s in "ab":
            lines += [call(session=s), tool(session=s, args={"command": "pytest"}, ok=False)]
        self.assertNotIn("RELEARN", self.codes(lines))

    def test_flood_carried(self):
        lines = [call(), tool(result_tokens=7000), call(), call()]
        t = [x for x in tab.find_tells(parse(lines)) if x["code"] == "FLOOD"][0]
        self.assertEqual(t["tokens"], 14000)

    def test_flood_last_call_counts_once(self):
        lines = [call(), tool(result_tokens=7000)]
        t = [x for x in tab.find_tells(parse(lines)) if x["code"] == "FLOOD"][0]
        self.assertEqual(t["tokens"], 7000)

    def test_reread(self):
        r = tool(name="Read", args={"file_path": "a.py"}, result_tokens=100)
        self.assertIn("REREAD", self.codes([call(), r, r, r]))

    def test_reread_other_tool_ignored(self):
        r = tool(name="Grep", args={"path": "a.py"})
        self.assertNotIn("REREAD", self.codes([call(), r, r, r]))

    def test_cache_break(self):
        self.assertIn("CACHE_BREAK", self.codes([call(cw=5000), call(cw=4000, cr=1000)]))

    def test_first_call_not_cache_break(self):
        self.assertNotIn("CACHE_BREAK", self.codes([call(cw=50000)]))

    def test_cache_break_small_ignored(self):
        self.assertNotIn("CACHE_BREAK", self.codes([call(cw=5000), call(cw=900, cr=100)]))

    def test_long_haul(self):
        t = [x for x in tab.find_tells(parse([call(cr=130000), call(cr=125000)])) if x["code"] == "LONG_HAUL"][0]
        self.assertEqual(t["tokens"], 15020)

    def test_heavy_start_majority(self):
        lines = [call(session="a", cw=30000), call(session="b", cw=30000), call(session="c", cw=10)]
        self.assertIn("HEAVY_START", self.codes(lines))

    def test_heavy_start_minority(self):
        lines = [call(session="a", cw=30000), call(session="b", cw=10), call(session="c", cw=10)]
        self.assertNotIn("HEAVY_START", self.codes(lines))

    def test_idle_tool(self):
        lines = []
        for s in "abcde":
            lines += [call(session=s), tool(session=s, name="Bash")]
        tells = tab.find_tells(parse(lines), None, {"Bash": 100, "WebFetch": 50})
        idle = [t["what"] for t in tells if t["code"] == "IDLE_TOOL"]
        self.assertEqual(idle, ["WebFetch"])

    def test_idle_tool_needs_enough_sessions(self):
        lines = [call(session="a"), tool(session="a")]
        tells = tab.find_tells(parse(lines), None, {"WebFetch": 50})
        self.assertFalse([t for t in tells if t["code"] == "IDLE_TOOL"])

    def test_thresholds_override(self):
        bad = tool(args={"command": "make"}, ok=False)
        self.assertIn("LOOP", self.codes([call(), bad, call(), bad], th={"loop": 2}))

    def test_ranked_by_tokens(self):
        lines = [call(), tool(result_tokens=7000), call(), tool(result_tokens=9000), call()]
        tells = tab.find_tells(parse(lines))
        self.assertGreaterEqual(tells[0]["tokens"], tells[-1]["tokens"])

    def test_every_tell_has_lever_and_undo(self):
        bad = tool(args={"command": "make"}, ok=False)
        for t in tab.find_tells(parse([call(cw=30000), bad, call(), bad, call(), bad, tool(result_tokens=9000)])):
            self.assertTrue(t["lever"] and t["undo"])

    def test_render_empty(self):
        self.assertIn("No tells", tab.render_tells([], 5))

    def test_render_limit(self):
        tells = [{"code": "FLOOD", "session": "s", "what": "x", "count": 1, "lines": [1], "tokens": 1, "lever": "l", "undo": "u"}] * 3
        self.assertIn("2 more not shown", tab.render_tells(tells, 1))

    def test_read_loaded(self):
        fd, p = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.write("# c\nBash 900\nWebFetch\nOdd x\n")
        self.assertEqual(tab.read_loaded(p), {"Bash": 900, "WebFetch": None, "Odd": None})


def sess(prefix, n, size):
    lines = []
    for i in range(n):
        lines.append(call(session="%s%d" % (prefix, i), fresh=size))
    return parse(lines)


class TrialTests(unittest.TestCase):
    def test_not_enough(self):
        r = tab.trial(sess("b", 3, 100), sess("a", 3, 50))
        self.assertEqual(r["verdict"], "NOT_ENOUGH")

    def test_keep_unverified(self):
        r = tab.trial(sess("b", 5, 100), sess("a", 5, 50))
        self.assertEqual(r["verdict"], "KEEP_UNVERIFIED")

    def test_no_gain(self):
        r = tab.trial(sess("b", 5, 100), sess("a", 5, 97))
        self.assertEqual(r["verdict"], "NO_GAIN")

    def test_keep_with_outcomes(self):
        oc = {"b%d" % i: True for i in range(5)}
        oc.update({"a%d" % i: True for i in range(5)})
        r = tab.trial(sess("b", 5, 100), sess("a", 5, 50), outcomes=oc)
        self.assertEqual(r["verdict"], "KEEP")

    def test_revert_when_quality_drops(self):
        oc = {"b%d" % i: True for i in range(5)}
        oc.update({"a%d" % i: i > 0 for i in range(5)})
        r = tab.trial(sess("b", 5, 100), sess("a", 5, 50), outcomes=oc)
        self.assertEqual(r["verdict"], "REVERT")

    def test_tolerance(self):
        oc = {"b%d" % i: True for i in range(5)}
        oc.update({"a%d" % i: i > 0 for i in range(5)})
        r = tab.trial(sess("b", 5, 100), sess("a", 5, 50), outcomes=oc, tolerance=0.25)
        self.assertEqual(r["verdict"], "KEEP")

    def test_outcomes_missing(self):
        r = tab.trial(sess("b", 5, 100), sess("a", 5, 50), outcomes={"b0": True})
        self.assertEqual(r["verdict"], "NOT_ENOUGH")

    def test_priced_unit(self):
        r = tab.trial(sess("b", 5, 100), sess("a", 5, 50), card=PRICES)
        self.assertEqual(r["unit"], "usd")

    def test_unpriced_model_stops(self):
        with self.assertRaises(SystemExit):
            tab.trial(sess("b", 5, 100), sess("a", 5, 50), card={"as_of": "x", "models": {"zz": PRICES["models"]["m"]}})

    def test_read_outcomes(self):
        fd, p = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as fh:
            fh.write("session,passed\na,1\nb,no\nc,\n")
        self.assertEqual(tab.read_outcomes(p), {"a": True, "b": False})

    def test_render(self):
        r = tab.trial(sess("b", 5, 100), sess("a", 5, 50))
        self.assertIn("KEEP_UNVERIFIED", tab.render_trial(r, "x"))


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        with redirect_stdout(io.StringIO()):
            tab.main(["demo", cls.tmp])

    def run_cli(self, args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = tab.main(args)
        return code, buf.getvalue()

    def test_demo_marked_synthetic(self):
        with open(os.path.join(self.tmp, "README.txt")) as fh:
            self.assertIn("SYNTHETIC", fh.read())

    def test_bill(self):
        code, out = self.run_cli(["bill", os.path.join(self.tmp, "sessions")])
        self.assertEqual(code, 0)
        self.assertIn("4 sessions", out)

    def test_bill_json(self):
        _code, out = self.run_cli(["bill", os.path.join(self.tmp, "sessions"), "--json",
                                   "--prices", os.path.join(self.tmp, "prices.example.json")])
        self.assertIsNotNone(json.loads(out)["totals"]["cost"])

    def test_tells_finds_all_demo_kinds(self):
        code, out = self.run_cli(["tells", os.path.join(self.tmp, "sessions"), "--json",
                                  "--loaded", os.path.join(self.tmp, "loaded_tools.txt"), "--idle-sessions", "4"])
        self.assertEqual(code, 1)
        kinds = set(t["code"] for t in json.loads(out))
        self.assertEqual(kinds, {"LOOP", "RELEARN", "FLOOD", "REREAD", "CACHE_BREAK", "HEAVY_START", "IDLE_TOOL"})

    def test_trial_keep(self):
        code, out = self.run_cli(["trial", "--before", os.path.join(self.tmp, "before"),
                                  "--after", os.path.join(self.tmp, "after"),
                                  "--outcomes", os.path.join(self.tmp, "outcomes.csv")])
        self.assertEqual(code, 0)
        self.assertIn("KEEP", out)

    def test_missing_path(self):
        with self.assertRaises(SystemExit):
            tab.main(["bill", os.path.join(self.tmp, "nope")])

    def test_file_merge_same_session(self):
        d = tempfile.mkdtemp()
        for i in range(2):
            with open(os.path.join(d, "p%d.jsonl" % i), "w") as fh:
                fh.write(call(session="same") + "\n")
        self.assertEqual(len(tab.load_paths([d])), 1)
        self.assertEqual(len(tab.load_paths([d])[0].calls), 2)


if __name__ == "__main__":
    unittest.main()
