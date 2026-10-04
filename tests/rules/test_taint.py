"""unvalidated-input-reaches-sink: outside input reaching a dangerous call unchecked.

The cases come from the full Magellan's tests/python/test_dataflow.py, checked here as a
change: the code below is new, so every flow through it is the change's.
"""

import textwrap
import unittest

from magellan_lite.engine import check_files

HEAD = """\
import os, sys, subprocess, sqlite3, pickle, yaml, re, shlex, html
from flask import Flask, request, redirect
app = Flask(__name__)
ALLOWED = ("a", "b")
"""


def flows(src: str, before: str | None = None, files: dict | None = None) -> list:
    after = {"app.py": HEAD + textwrap.dedent(src), **(files or {})}
    old = {"app.py": HEAD + textwrap.dedent(before)} if before is not None else {}
    return [f for f in check_files(old, after).findings
            if f.rule == "unvalidated-input-reaches-sink"]


def kinds(src: str, **kw) -> list:
    """The sinks reached, by what they do."""
    words = {"shell": "exec", "Python code": "eval", "SQL": "sql", "unpickles": "deserialize",
             "file path": "path", "URL": "ssrf", "redirects": "redirect", "HTML": "html"}
    out = []
    for f in flows(src, **kw):
        out += [k for w, k in words.items() if w in f.message]
    return sorted(out)


class Basics(unittest.TestCase):
    def test_request_field_into_a_shell_command(self):
        [f] = flows("""
            @app.route("/")
            def h():
                os.system("ls " + request.args["d"])
        """)
        self.assertEqual((f.severity, f.path, f.line), ("critical", "app.py", 8))
        self.assertIn("request data (request.args) reaches os.system() in h()", f.message)
        self.assertIn("parameterized", flows("""
            @app.route("/")
            def h(cur):
                cur.execute("select " + request.args["u"])
        """)[0].fix)

    def test_a_flow_across_helpers_names_its_path(self):
        [f] = flows("""
            def run(cmd):
                return subprocess.run(cmd, shell=True)

            def build(name):
                return "ls " + name

            @app.route("/")
            def h():
                return run(build(request.args["n"]))
        """)
        self.assertEqual(f.line, 7)                        # the sink, in run()
        self.assertIn("h() hands it to run()", f.detail)

    def test_handler_parameters_are_sources_plain_parameters_are_not(self):
        self.assertEqual(kinds("""
            @app.route("/<name>")
            def h(name):
                os.system(name)
        """), ["exec"])
        self.assertEqual(kinds("""
            def helper(name):
                os.system(name)
        """), [])
        self.assertEqual(kinds("""
            @app.get("/items/<item_id>")
            def h(item_id: int):
                os.system("echo %d" % item_id)
        """), [])                                          # already converted to int

    def test_recursion_and_construction(self):
        self.assertEqual(kinds("""
            def a(x): return b(x)
            def b(x): return c(x)
            def c(x):
                if x: return a(x[1:])
                os.system(x)

            @app.route("/")
            def h():
                return a(request.args["q"])
        """), ["exec"])
        self.assertEqual(kinds("""
            class Runner:
                def __init__(self, cmd):
                    os.system(cmd)

            @app.route("/")
            def h():
                Runner(request.args["c"])
        """), ["exec"])


class Clearing(unittest.TestCase):
    def test_conversion_and_sanitizers_for_their_own_sink(self):
        self.assertEqual(kinds("""
            @app.route("/")
            def h():
                n = int(request.args["n"])
                os.system("sleep %d" % n)
        """), [])
        self.assertEqual(kinds("""
            @app.route("/")
            def h():
                os.system("echo " + shlex.quote(request.args["v"]))
        """), [])
        self.assertEqual(kinds("""
            @app.route("/")
            def h(cur):
                v = shlex.quote(request.args["v"])
                cur.execute("select " + v)
        """), ["sql"])                                     # shlex.quote is no help for SQL
        self.assertEqual(kinds("""
            @app.route("/")
            def h():
                return redirect(html.escape(request.args["u"]))
        """), ["redirect"])

    def test_guards(self):
        for guard in ("if v in ALLOWED:", "if v.isdigit():", 'if re.fullmatch(r"[a-z]+", v):',
                      "if validate_name(v):"):
            with self.subTest(guard=guard):
                self.assertEqual(kinds(f"""
                    @app.route("/")
                    def h():
                        v = request.args["v"]
                        {guard}
                            os.system(v)
                """), [])
        self.assertEqual(kinds("""
            @app.route("/")
            def h():
                v = request.args["v"]
                if not v.isalnum():
                    raise ValueError("bad")
                os.system("echo " + v)
        """), [])
        self.assertEqual(kinds("""
            @app.route("/")
            def h():
                v = request.args["v"]
                if v in ALLOWED:
                    pass
                os.system(v)
        """), ["exec"])                                    # the guard covers its own block only

    def test_reassignment(self):
        self.assertEqual(kinds("""
            @app.route("/")
            def h():
                v = request.args["v"]
                v = "constant"
                os.system(v)
        """), [])
        self.assertEqual(kinds("""
            @app.route("/")
            def h(flag):
                v = request.args["v"]
                if flag:
                    v = "constant"
                os.system(v)
        """), ["exec"])


class Sinks(unittest.TestCase):
    def test_parameterized_sql_is_safe_string_building_is_not(self):
        self.assertEqual(kinds("""
            @app.route("/")
            def h(cur):
                cur.execute("select * from t where u = %s", (request.args["u"],))
        """), [])
        for build in ('"select " + u', 'f"select {u}"', '"select %s" % u',
                      '"select {}".format(u)'):
            with self.subTest(build=build):
                self.assertEqual(kinds(f"""
                    @app.route("/")
                    def h(cur):
                        u = request.args["u"]
                        cur.execute({build})
                """), ["sql"])

    def test_an_argument_list_without_a_shell_only_matters_for_the_program(self):
        self.assertEqual(kinds("""
            @app.route("/")
            def h():
                subprocess.run(["ls", request.args["d"]])
        """), [])
        self.assertEqual(kinds("""
            @app.route("/")
            def h():
                subprocess.run([request.args["prog"], "-l"])
        """), ["exec"])
        self.assertEqual(kinds("""
            from subprocess import run as srun
            @app.route("/")
            def h():
                srun(request.args["a"], shell=True)
        """), ["exec"])

    def test_loaders_paths_and_the_rest(self):
        self.assertEqual(kinds("""
            @app.route("/")
            def h():
                return yaml.load(request.data, Loader=yaml.SafeLoader)
        """), [])
        self.assertEqual(kinds("""
            @app.route("/")
            def h():
                return open("/etc/config").read()
        """), [])
        self.assertEqual(kinds("""
            @app.route("/f")
            def h(name):
                return open(name).read()
        """), ["path"])
        self.assertEqual(kinds("""
            @app.route("/")
            def h():
                eval(request.args["e"])
                pickle.loads(request.data)
                yaml.load(request.data)
                redirect(request.args["next"])
        """), ["deserialize", "deserialize", "eval", "redirect"])


class Sources(unittest.TestCase):
    def test_command_line_input_is_a_level_lower_and_only_for_code_and_shells(self):
        [f] = flows("""
            def main():
                os.system("echo " + sys.argv[1])
        """)
        self.assertEqual(f.severity, "high")
        self.assertEqual(kinds("""
            def main():
                return open(sys.argv[1]).read()
        """), [])                                          # a tool opening what it was given

    def test_request_data_needs_a_web_framework(self):
        src = "import os\n\n\ndef f(request):\n    os.system(request.args['x'])\n"
        self.assertEqual([f.rule for f in check_files({}, {"jobs.py": src}).findings
                          if f.rule == "unvalidated-input-reaches-sink"], [])

    def test_a_network_response_is_a_source(self):
        self.assertEqual(kinds("""
            import requests
            def sync():
                data = requests.get("https://x.example/cmd").text
                os.system(data)
        """), ["exec"])

    def test_comprehensions_and_method_calls_carry_it(self):
        self.assertEqual(kinds("""
            @app.route("/")
            def h():
                parts = [p.strip() for p in request.args["q"].split(",")]
                os.system(" ".join(parts))
        """), ["exec"])


class Change(unittest.TestCase):
    BASE = """
        def run(c):
            return subprocess.run(c, shell=True)

        def other():
            return 1

        @app.route("/")
        def h():
            return run(request.args["n"])
    """

    def test_only_flows_through_edited_code_are_reported(self):
        self.assertEqual(flows(self.BASE.replace("return 1", "return 2"), before=self.BASE), [])
        [f] = flows(self.BASE.replace("shell=True)", "shell=True, timeout=5)"),
                    before=self.BASE)
        self.assertEqual(f.severity, "critical")

    def test_test_files_are_left_alone(self):
        test = "import os\nfrom flask import request\n\n\ndef test_x():\n" \
               "    os.system(request.args['a'])\n"
        self.assertEqual(flows("", files={"tests/test_app.py": test}), [])


if __name__ == "__main__":
    unittest.main()
