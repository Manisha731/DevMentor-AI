"""Structural code analysis with tree-sitter.

Pure functions: source text in, list of Issue out. No DB, no network, so it is
easy to test. The Code Agent (code_agent.py) feeds it files from GitHub and
stores the results as `findings` rows.

Design rule (Decision 14): this module reports FACTS about code structure
(e.g. "function X has cyclomatic complexity 14"). It never judges or advises;
that is the Review/Mentor agents' job.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

# ---- thresholds (facts are only emitted past these) ----
COMPLEXITY_THRESHOLD = 10
NESTING_THRESHOLD = 4
LONG_FUNCTION_LINES = 60

EXT_TO_LANG = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
}

_LANGS = {
    "python": Language(tree_sitter_python.language()),
    "javascript": Language(tree_sitter_javascript.language()),
    "typescript": Language(tree_sitter_typescript.language_typescript()),
    "tsx": Language(tree_sitter_typescript.language_tsx()),
    "java": Language(tree_sitter_java.language()),
}

FUNC_TYPES = {
    "python": {"function_definition"},
    "javascript": {"function_declaration", "method_definition", "arrow_function",
                   "function_expression", "function", "generator_function_declaration"},
    "java": {"method_declaration", "constructor_declaration"},
}
FUNC_TYPES["typescript"] = FUNC_TYPES["tsx"] = FUNC_TYPES["javascript"]

# nodes that add +1 to cyclomatic complexity
BRANCH_TYPES = {
    "python": {"if_statement", "elif_clause", "for_statement", "while_statement",
               "except_clause", "conditional_expression", "boolean_operator",
               "case_clause", "list_comprehension", "dictionary_comprehension",
               "set_comprehension", "generator_expression"},
    "javascript": {"if_statement", "for_statement", "for_in_statement", "while_statement",
                   "do_statement", "switch_case", "catch_clause", "ternary_expression",
                   "&&", "||", "??"},
    "java": {"if_statement", "for_statement", "enhanced_for_statement", "while_statement",
             "do_statement", "switch_label", "catch_clause", "ternary_expression",
             "&&", "||"},
}
BRANCH_TYPES["typescript"] = BRANCH_TYPES["tsx"] = BRANCH_TYPES["javascript"]

# nodes that increase nesting depth
NEST_TYPES = {
    "python": {"if_statement", "for_statement", "while_statement", "try_statement",
               "with_statement", "match_statement"},
    "javascript": {"if_statement", "for_statement", "for_in_statement", "while_statement",
                   "do_statement", "switch_statement", "try_statement"},
    "java": {"if_statement", "for_statement", "enhanced_for_statement", "while_statement",
             "do_statement", "switch_expression", "switch_statement", "try_statement"},
}
NEST_TYPES["typescript"] = NEST_TYPES["tsx"] = NEST_TYPES["javascript"]

TERMINATORS = {"return_statement", "raise_statement", "throw_statement",
               "break_statement", "continue_statement"}

COMMENT_TYPES = {"comment", "line_comment", "block_comment"}
MARKER_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s]*(.*)", re.IGNORECASE)


@dataclass
class Issue:
    finding_type: str          # stub_function | todo_marker | high_complexity | deep_nesting
                               # | long_function | empty_catch | bare_except | dead_code
    severity: str              # info | low | medium | high
    line: int                  # 1-based
    function_name: str | None
    description: str
    details: dict = field(default_factory=dict)
    end_line: int | None = None  # last line the issue spans (functions); None = same as line


def language_for(path: str) -> str | None:
    dot = path.rfind(".")
    return EXT_TO_LANG.get(path[dot:].lower()) if dot != -1 else None


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _func_name(node: Node, src: bytes, parent: Node | None) -> str:
    name = node.child_by_field_name("name")
    if name is not None:
        return _text(name, src)
    # const foo = () => {}  /  foo: function() {}
    if parent is not None and parent.type in {"variable_declarator", "pair",
                                              "public_field_definition"}:
        n = parent.child_by_field_name("name") or parent.child_by_field_name("key")
        if n is not None:
            return _text(n, src)
    return "<anonymous>"


def _walk(node: Node):
    yield node
    for c in node.children:
        yield from _walk(c)


def _complexity(func: Node, lang: str) -> int:
    branch = BRANCH_TYPES[lang]
    ftypes = FUNC_TYPES[lang]
    total = 1

    def visit(n: Node, top: bool):
        nonlocal total
        # nested functions are measured on their own
        if not top and n.type in ftypes:
            return
        if n.type in branch:
            total += 1
        for c in n.children:
            visit(c, False)

    visit(func, True)
    return total


def _max_nesting(func: Node, lang: str) -> tuple[int, int]:
    """Return (max depth, line where it first reached that depth)."""
    nest = NEST_TYPES[lang]
    ftypes = FUNC_TYPES[lang]
    best = (0, func.start_point[0] + 1)

    def visit(n: Node, depth: int, top: bool):
        nonlocal best
        if not top and n.type in ftypes:
            return
        d = depth
        if n.type in nest:
            d = depth + 1
            if d > best[0]:
                best = (d, n.start_point[0] + 1)
        for c in n.children:
            visit(c, d, False)

    visit(func, 0, True)
    return best


def _body(func: Node) -> Node | None:
    return func.child_by_field_name("body")


def _is_stub(func: Node, lang: str, src: bytes) -> str | None:
    """Return a reason string if the function body is a placeholder, else None."""
    body = _body(func)
    if body is None:
        return None  # abstract/interface method: not a stub
    stmts = [c for c in body.named_children if c.type not in COMMENT_TYPES]
    if lang == "python":
        # skip a leading docstring
        if stmts and stmts[0].type == "expression_statement" and \
                stmts[0].named_children and stmts[0].named_children[0].type == "string":
            stmts = stmts[1:]
        if not stmts:
            return "body is empty (docstring only)"
        if len(stmts) == 1:
            s = stmts[0]
            t = _text(s, src).strip()
            if s.type == "pass_statement":
                return "body is only `pass`"
            if t == "...":
                return "body is only `...`"
            if s.type == "raise_statement" and "NotImplementedError" in t:
                return "raises NotImplementedError"
        return None
    # brace languages
    if not stmts:
        return "body is empty"
    if len(stmts) == 1 and stmts[0].type in {"throw_statement"}:
        t = _text(stmts[0], src)
        if re.search(r"not\s*implemented|unsupported", t, re.IGNORECASE):
            return "throws a not-implemented error"
    return None


def _find_python_except(root: Node, src: bytes, issues: list[Issue]):
    for n in _walk(root):
        if n.type != "except_clause":
            continue
        header = _text(n, src).split("\n", 1)[0].strip()
        named = [c for c in n.named_children if c.type != "block"]
        block = next((c for c in n.named_children if c.type == "block"), None)
        if not named:  # `except:` with no exception type
            issues.append(Issue("bare_except", "medium", n.start_point[0] + 1, None,
                                "bare `except:` catches every exception including KeyboardInterrupt",
                                {"header": header}))
        if block is not None:
            body = [c for c in block.named_children if c.type not in COMMENT_TYPES]
            if len(body) == 1 and body[0].type == "pass_statement":
                issues.append(Issue("empty_catch", "medium", n.start_point[0] + 1, None,
                                    "exception is caught and silently ignored (`pass`)",
                                    {"header": header}))


def _find_brace_catch(root: Node, src: bytes, issues: list[Issue]):
    for n in _walk(root):
        if n.type != "catch_clause":
            continue
        body = n.child_by_field_name("body")
        if body is not None and not [c for c in body.named_children if c.type not in COMMENT_TYPES]:
            issues.append(Issue("empty_catch", "medium", n.start_point[0] + 1, None,
                                "catch block is empty, so the error is silently swallowed", {}))


def _find_dead_code(root: Node, issues: list[Issue], lang: str):
    """Statements that follow return/raise/throw/break/continue in the same block."""
    for n in _walk(root):
        if n.type not in {"block", "statement_block"}:
            continue
        kids = [c for c in n.named_children if c.type not in COMMENT_TYPES]
        for i, c in enumerate(kids[:-1]):
            if c.type in TERMINATORS:
                nxt = kids[i + 1]
                issues.append(Issue("dead_code", "low", nxt.start_point[0] + 1, None,
                                    f"unreachable code after `{c.type.replace('_statement', '')}`",
                                    {"after_line": c.start_point[0] + 1}))
                break


def _find_markers(root: Node, src: bytes, issues: list[Issue]):
    for n in _walk(root):
        if n.type not in COMMENT_TYPES:
            continue
        m = MARKER_RE.search(_text(n, src))
        if m:
            issues.append(Issue("todo_marker", "info", n.start_point[0] + 1, None,
                                f"{m.group(1).upper()} marker: {m.group(2).strip()[:120] or '(no text)'}",
                                {"marker": m.group(1).upper()}))


def analyze_source(path: str, source: str) -> list[Issue]:
    lang = language_for(path)
    if lang is None:
        return []
    src = source.encode("utf-8")
    parser = Parser(_LANGS[lang])
    tree = parser.parse(src)
    root = tree.root_node
    issues: list[Issue] = []

    ftypes = FUNC_TYPES[lang]
    stack = [(root, None)]
    while stack:
        node, parent = stack.pop()
        if node.type in ftypes:
            name = _func_name(node, src, parent)
            line = node.start_point[0] + 1
            end = node.end_point[0] + 1
            length = end - line + 1

            reason = _is_stub(node, lang, src)
            if reason:
                issues.append(Issue("stub_function", "medium", line, name,
                                    f"`{name}` looks unfinished: {reason}", {"reason": reason}, end_line=end))
            cx = _complexity(node, lang)
            if cx >= COMPLEXITY_THRESHOLD:
                sev = "high" if cx >= 20 else "medium"
                issues.append(Issue("high_complexity", sev, line, name,
                                    f"`{name}` has cyclomatic complexity {cx}",
                                    {"complexity": cx, "threshold": COMPLEXITY_THRESHOLD}, end_line=end))
            depth, dline = _max_nesting(node, lang)
            if depth >= NESTING_THRESHOLD:
                issues.append(Issue("deep_nesting", "medium", dline, name,
                                    f"`{name}` nests control flow {depth} levels deep",
                                    {"depth": depth, "threshold": NESTING_THRESHOLD}, end_line=end))
            if length >= LONG_FUNCTION_LINES:
                issues.append(Issue("long_function", "low", line, name,
                                    f"`{name}` is {length} lines long",
                                    {"lines": length, "threshold": LONG_FUNCTION_LINES}, end_line=end))
        for c in reversed(node.children):
            stack.append((c, node))

    if lang == "python":
        _find_python_except(root, src, issues)
    else:
        _find_brace_catch(root, src, issues)
    _find_dead_code(root, issues, lang)
    _find_markers(root, src, issues)

    if root.has_error:
        issues.append(Issue("syntax_error", "high", 1, None,
                            "file has syntax errors (tree-sitter could not parse it cleanly)", {}))
    issues.sort(key=lambda i: (i.line, i.finding_type))
    return issues
