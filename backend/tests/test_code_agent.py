import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# code_agent imports app.models; if the full app config isn't available (e.g. bare CI), stub it.
import types  # noqa: E402

try:
    import app.models  # noqa: F401
except Exception:
    m = types.ModuleType("app.models")
    for n in ("AgentRun", "Commit", "Finding", "Repo"):
        setattr(m, n, type(n, (), {}))
    sys.modules["app.models"] = m

from app.code_agent import ChangedFile, added_lines, analyze_files  # noqa: E402

PATCH = """@@ -1,3 +1,4 @@
 def a():
     return 1
+# TODO: new thing
 x = 1
@@ -10,2 +11,3 @@ def b():
 y = 2
-z = 3
+z = 4
+w = 5
"""


def test_added_lines():
    assert added_lines(PATCH) == {3, 12, 13}
    assert added_lines(None) is None
    assert added_lines("") == set()


OLD_FILE = '''def untouched():
    pass

def edited(x):
    return x  # edited line
'''


def test_only_changed_lines_are_reported():
    # patch only adds line 5; `untouched` is an old stub and must NOT be reported
    patch = "@@ -4,1 +4,2 @@ def edited(x):\n def edited(x):\n+    return x  # edited line\n"
    res, _ = analyze_files([ChangedFile("a.py", "modified", patch)], lambda p: OLD_FILE)
    assert res == []


def test_function_touched_by_change_is_reported():
    src = "def f():\n    pass\n"
    patch = "@@ -1,1 +1,2 @@\n def f():\n+    pass\n"
    res, _ = analyze_files([ChangedFile("a.py", "modified", patch)], lambda p: src)
    assert [i.finding_type for _, i in res] == ["stub_function"]


def test_new_file_analyzed_fully_and_skips_are_reported():
    files = [ChangedFile("new.py", "added", None), ChangedFile("README.md", "modified", "x"),
             ChangedFile("old.py", "removed", None), ChangedFile("big.py", "modified", None)]
    res, skipped = analyze_files(files, lambda p: None if p == "big.py" else "def f():\n    pass\n")
    assert [p for p, _ in res] == ["new.py"]
    assert dict(skipped) == {"README.md": "unsupported file type", "old.py": "removed",
                             "big.py": "could not fetch / too large"}
