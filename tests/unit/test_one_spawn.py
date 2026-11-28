"""One process start: nothing in the doris package starts a process except supervisor._spawn.

SAFETY.md's Deploy row says no tool of hers runs a command, installs anything or starts her, and the
one process she starts is her own worker, under the supervisor. This test reads every .py file of
src/doris as source (with ast; nothing is imported or run) and fails on every way the standard
library offers to start a process, outside supervisor._spawn:

- importing subprocess (the supervisor alone may, and calls Popen only inside _spawn),
  multiprocessing or pty, or anything from them by name;
- os.system, os.popen, os.exec*, os.spawn*, os.fork, os.forkpty, os.posix_spawn, os.posix_spawnp
  and os.startfile, by attribute, by `from os import` or by getattr;
- asyncio.create_subprocess_exec and _shell, asyncio.subprocess, and a ProcessPoolExecutor;
- the ways to reach those without naming them: importlib.import_module or __import__ with anything
  but a relative name inside the package, eval, exec and compile, and supervisor's own subprocess
  or _spawn reached from another module.

src/secret_scan.py is not in the package: it is the commit hook's and CI's secret scan, never part
of a run, and it runs git.

Each planted case copies the package to a temp folder, plants one call in one file and shows the
scan names that file and line, so the clean result on the real tree is not a scan that sees nothing.
"""

import ast
import os
import shutil
import tempfile
import unittest

from doris import paths

PACKAGE = os.path.join(paths.SRC, "doris")
SUPERVISOR = "supervisor.py"
SPAWN = "_spawn"

PROCESS_MODULES = frozenset({"subprocess", "multiprocessing", "pty"})
SUBPROCESS_STARTS = frozenset({"Popen", "run", "call", "check_call", "check_output", "getoutput",
                               "getstatusoutput"})
OS_STARTS = frozenset({
    "system", "popen", "fork", "forkpty", "startfile", "posix_spawn", "posix_spawnp",
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
})
OTHER_STARTS = frozenset({"asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
                          "asyncio.subprocess", "concurrent.futures.ProcessPoolExecutor",
                          "concurrent.futures.process"})
HIDDEN_BUILTINS = frozenset({"eval", "exec", "compile", "__import__"})
IMPORTERS = frozenset({"importlib.import_module", "importlib.__import__", "builtins.__import__"})


def _top(dotted):
    return dotted.split(".", 1)[0]


def _relative(node):
    """A module name that can only reach inside the package: "." + name, or a constant like ".x"."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.startswith(".")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _relative(node.left)
    return False


class _Scan(ast.NodeVisitor):
    def __init__(self, rel):
        self.rel = rel
        self.is_supervisor = rel == SUPERVISOR
        self.names = {}          # a local name -> the dotted module path it stands for
        self.functions = []      # the enclosing function names, outermost first
        self.found = []          # (line, what)
        self.spawn_popens = 0    # Popen calls inside supervisor._spawn

    # --- names --------------------------------------------------------------------------------
    def resolve(self, node):
        """The dotted path a Name or Attribute chain stands for, through the module's imports."""
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        head = self.names.get(node.id)
        if head is None:
            return None
        return ".".join([head] + list(reversed(parts)))

    def flag(self, node, what):
        self.found.append((node.lineno, what))

    def in_spawn(self):
        return self.is_supervisor and self.functions[:1] == [SPAWN]

    # --- what a dotted path may be --------------------------------------------------------------
    def check_path(self, node, path):
        top = _top(path)
        last = path.rsplit(".", 1)[-1]
        if top in ("multiprocessing", "pty"):
            self.flag(node, path)
        elif top == "subprocess":
            if not self.is_supervisor:
                self.flag(node, path)
            elif last in SUBPROCESS_STARTS and not self.in_spawn():
                self.flag(node, "%s outside %s" % (path, SPAWN))
        elif top == "os" and last in OS_STARTS:
            self.flag(node, path)
        elif any(path == p or path.startswith(p + ".") for p in OTHER_STARTS):
            self.flag(node, path)

    # --- the visitor ------------------------------------------------------------------------------
    def visit_Import(self, node):
        for alias in node.names:
            name = alias.name
            if alias.asname:
                self.names[alias.asname] = name
            else:
                self.names[_top(name)] = _top(name)
            if _top(name) in PROCESS_MODULES and not (self.is_supervisor and name == "subprocess"):
                self.flag(node, "import " + name)
            elif any(name == p or name.startswith(p + ".") for p in OTHER_STARTS):
                self.flag(node, "import " + name)

    def visit_ImportFrom(self, node):
        module = node.module or ""
        if node.level:                       # inside the package: from . import x, from ..shapes import y
            for alias in node.names:
                self.names.pop(alias.asname or alias.name, None)
                if not self.is_supervisor and (alias.name == SPAWN or alias.name in PROCESS_MODULES):
                    self.flag(node, "from %s%s import %s" % ("." * node.level, module, alias.name))
            return
        if _top(module) in PROCESS_MODULES:
            self.flag(node, "from %s import ..." % module)
        for alias in node.names:
            path = "%s.%s" % (module, alias.name)
            self.names[alias.asname or alias.name] = path
            if _top(module) not in PROCESS_MODULES:
                self.check_path(node, path)

    def visit_FunctionDef(self, node):
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Attribute(self, node):
        if not self.is_supervisor and (node.attr == SPAWN or node.attr in PROCESS_MODULES):
            self.flag(node, "." + node.attr)    # reached through another module: supervisor.subprocess, x._spawn
        path = self.resolve(node)
        if path is not None:
            self.check_path(node, path)
            return                            # the chain's inner names are part of this one path
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Name) and func.id in HIDDEN_BUILTINS and func.id not in self.names:
            self.flag(node, func.id + "()")
        path = self.resolve(func) if isinstance(func, (ast.Name, ast.Attribute)) else None
        if path in IMPORTERS and not (node.args and _relative(node.args[0])):
            self.flag(node, "%s() of a name outside the package" % path)
        if path == "subprocess.Popen" and self.in_spawn():
            self.spawn_popens += 1
        if isinstance(func, ast.Name) and func.id == "getattr" and "getattr" not in self.names and node.args:
            target = self.resolve(node.args[0]) if isinstance(node.args[0], (ast.Name, ast.Attribute)) else None
            if target is not None and (target == "os" or _top(target) in PROCESS_MODULES):
                attr = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
                    self.flag(node, "getattr(%s, <not a constant>)" % target)
                else:
                    self.check_path(node, "%s.%s" % (target, attr.value))
        self.generic_visit(node)


def scan_source(source, rel):
    """(findings, Popen calls inside supervisor._spawn) for one file's source."""
    v = _Scan(rel)
    v.visit(ast.parse(source, filename=rel))
    return ["%s:%d: %s" % (rel, line, what) for line, what in v.found], v.spawn_popens


def scan_tree(root):
    """Every .py file under root (a copy of the package, or the package itself), by path from root
    with forward slashes. Returns (findings, files scanned, Popen calls inside supervisor._spawn)."""
    findings, files, popens = [], [], 0
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            with open(path, encoding="utf-8") as fh:
                found, n = scan_source(fh.read(), rel)
            findings.extend(found)
            files.append(rel)
            popens += n
    return findings, files, popens


class OneSpawnTest(unittest.TestCase):
    def test_nothing_but_the_supervisor_starts_a_process(self):
        findings, _, _ = scan_tree(PACKAGE)
        self.assertEqual(findings, [], "a process start outside supervisor._spawn:\n" + "\n".join(findings))

    def test_the_supervisor_starts_its_worker_with_one_popen_inside_spawn(self):
        _, _, popens = scan_tree(PACKAGE)
        self.assertEqual(popens, 1)

    def test_the_scan_reads_every_module_of_the_package_and_its_adapters(self):
        _, files, _ = scan_tree(PACKAGE)
        self.assertIn("loop.py", files)
        self.assertIn(SUPERVISOR, files)
        self.assertIn("adapters/live.py", files)
        on_disk = sum(1 for _, _, names in os.walk(PACKAGE) for n in names if n.endswith(".py"))
        self.assertEqual(len(files), on_disk)


# Each case: the file to plant in, the lines to add at its end, and a word the finding must carry.
PLANTED = (
    ("loop.py", ["import subprocess", "subprocess.run(['true'])"], "subprocess"),
    ("gate.py", ["from subprocess import Popen"], "subprocess"),
    ("policy.py", ["import os", "def _planted():", "    os.system('true')"], "os.system"),
    ("model.py", ["import os as _o", "_o.execv('/bin/true', ['true'])"], "os.execv"),
    ("compose.py", ["from os import fork"], "os.fork"),
    ("digest.py", ["import os", "os.posix_spawn('/bin/true', ['true'], {})"], "os.posix_spawn"),
    ("templates.py", ["import os", "os.popen('true')"], "os.popen"),
    ("ledger.py", ["import os", "os.spawnv(os.P_WAIT, '/bin/true', ['true'])"], "os.spawnv"),
    ("ics.py", ["import multiprocessing"], "multiprocessing"),
    ("slots.py", ["import pty"], "pty"),
    ("parser.py", ["import asyncio", "asyncio.create_subprocess_exec('true')"], "create_subprocess_exec"),
    ("zones.py", ["import concurrent.futures", "concurrent.futures.ProcessPoolExecutor()"], "ProcessPoolExecutor"),
    ("holds.py", ["import importlib", "importlib.import_module('subprocess')"], "import_module"),
    ("mail.py", ["__import__('subprocess')"], "__import__"),
    ("screens.py", ["import os", "getattr(os, 'sys' + 'tem')('true')"], "getattr"),
    ("receipts.py", ["import os", "getattr(os, 'system')('true')"], "os.system"),
    ("why.py", ["exec('import subprocess')"], "exec"),
    ("adapters/live.py", ["import subprocess"], "subprocess"),
    ("loop.py", ["from . import supervisor", "supervisor.subprocess.Popen(['true'])"], "subprocess"),
    ("traps.py", ["from . import supervisor", "supervisor._spawn(['true'], None)"], "_spawn"),
    ("smoke.py", ["from .supervisor import _spawn"], "_spawn"),
    (SUPERVISOR, ["def _elsewhere():", "    subprocess.Popen(['true'])"], "outside _spawn"),
    (SUPERVISOR, ["def _elsewhere():", "    subprocess.check_output(['true'])"], "outside _spawn"),
)


class PlantedTest(unittest.TestCase):
    """The scan finds each planted start in a copy of the package, at the file and line it went in."""

    def plant(self, target, lines):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "doris")
        shutil.copytree(PACKAGE, root, ignore=shutil.ignore_patterns("__pycache__"))
        path = os.path.join(root, *target.split("/"))
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if not text.endswith("\n"):
            text += "\n"
        first = text.count("\n") + 1
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n".join(lines) + "\n")
        return root, list(range(first, first + len(lines)))

    def test_every_planted_start_is_found_at_its_file_and_line(self):
        for target, lines, word in PLANTED:
            with self.subTest(target=target, planted=lines[-1]):
                root, planted_lines = self.plant(target, lines)
                findings, _, _ = scan_tree(root)
                hits = [f for f in findings if f.startswith(target + ":")
                        and int(f.split(":")[1]) in planted_lines and word in f]
                self.assertTrue(hits, "not found: %r in %s; findings: %r" % (lines, target, findings))

    def test_a_clean_copy_has_no_findings(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "doris")
        shutil.copytree(PACKAGE, root, ignore=shutil.ignore_patterns("__pycache__"))
        self.assertEqual(scan_tree(root)[0], [])

    def test_ordinary_os_calls_and_relative_imports_are_not_findings(self):
        source = "\n".join([
            "import os", "import importlib", "import signal",
            "os.replace('a', 'b')", "os.kill(1, signal.SIGKILL)", "getattr(os, 'O_BINARY', 0)",
            "importlib.import_module('.gate', __package__)",
            "name = 'gate'", "importlib.import_module('.' + name, __package__)",
        ])
        self.assertEqual(scan_source(source, "loop.py")[0], [])


if __name__ == "__main__":
    unittest.main()
