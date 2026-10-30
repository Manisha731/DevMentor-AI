import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_analyzer import analyze_source, language_for  # noqa: E402


def types(issues):
    return [i.finding_type for i in issues]


PY_BAD = '''
# TODO: add validation
def todo_fn():
    pass

def not_done():
    raise NotImplementedError

def doc_only():
    """Does something."""

def swallow():
    try:
        x = 1
    except:
        pass

def dead():
    return 1
    print("never")

def gnarly(a, b, c):
    if a:
        for i in range(b):
            while c:
                if i and a:
                    if b or c:
                        return 1
    elif b:
        return 2
    elif c:
        return 3
    for x in a:
        if x > 1 and x < 5 or x == 9:
            pass
    return 0

def fine(x):
    return x + 1
'''


def test_python_findings():
    issues = analyze_source("a.py", PY_BAD)
    t = types(issues)
    assert t.count("stub_function") == 3          # pass, NotImplementedError, docstring-only
    assert "todo_marker" in t
    assert "bare_except" in t
    assert "empty_catch" in t
    assert "dead_code" in t
    assert "high_complexity" in t
    assert "deep_nesting" in t
    names = {i.function_name for i in issues if i.finding_type == "stub_function"}
    assert names == {"todo_fn", "not_done", "doc_only"}
    assert not any(i.function_name == "fine" for i in issues)


JS_BAD = '''
// FIXME: handle errors
function empty() {}
const arrow = () => { throw new Error("not implemented"); };
function swallow() { try { a(); } catch (e) {} }
function dead() { return 1; console.log("x"); }
const deep = (a) => {
  if (a) { for (;;) { while (a) { if (a) { if (a && a || a) { return 1; } } } } }
};
function ok(a) { return a + 1; }
'''


def test_javascript_findings():
    issues = analyze_source("a.js", JS_BAD)
    t = types(issues)
    assert t.count("stub_function") == 2
    assert "empty_catch" in t and "dead_code" in t and "todo_marker" in t
    assert "deep_nesting" in t
    assert {i.function_name for i in issues if i.finding_type == "stub_function"} == {"empty", "arrow"}


def test_typescript_and_tsx_parse():
    ts = "export function f(a: number): number { return a; }\n// TODO: x\n"
    assert types(analyze_source("a.ts", ts)) == ["todo_marker"]
    tsx = "export const C = () => { return <div/>; };\n"
    assert analyze_source("a.tsx", tsx) == []


JAVA_BAD = '''
class A {
  void stub() {}
  void swallow() { try { x(); } catch (Exception e) { } }
  int ok(int a) { return a; }
  abstract void abs();
}
'''


def test_java_findings():
    issues = analyze_source("A.java", JAVA_BAD)
    t = types(issues)
    assert "stub_function" in t and "empty_catch" in t
    assert {i.function_name for i in issues if i.finding_type == "stub_function"} == {"stub"}


def test_syntax_error_and_unknown_ext():
    assert "syntax_error" in types(analyze_source("a.py", "def (:\n"))
    assert analyze_source("README.md", "# TODO\n") == []
    assert language_for("x.tar.gz") is None
    assert language_for("Makefile") is None


def test_clean_file_has_no_issues():
    assert analyze_source("a.py", "def f(x):\n    return x * 2\n") == []
