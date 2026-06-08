from __future__ import annotations

import difflib
import importlib.util
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from services.repo_intelligence.repomap import RepoMap

HAS_FULL_REPOMAP_DEPS = all(
    importlib.util.find_spec(name) is not None
    for name in ("networkx", "grep_ast", "tree_sitter", "pygments")
)


class _FakeModel:
    def token_count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)


class _FakeIO:
    def __init__(self) -> None:
        self.outputs: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def read_text(self, fname: str) -> str:
        return Path(fname).read_text(encoding="utf-8", errors="replace")

    def tool_output(self, message: str = "") -> None:
        if message:
            self.outputs.append(message)

    def tool_warning(self, message: str = "") -> None:
        if message:
            self.warnings.append(message)

    def tool_error(self, message: str = "") -> None:
        if message:
            self.errors.append(message)


class _RepoMapTestCase(unittest.TestCase):
    def setUp(self):
        self.model = _FakeModel()
        self._repo_maps: list[RepoMap] = []

    def tearDown(self):
        while self._repo_maps:
            self._repo_maps.pop().close()

    def make_repo_map(self, root: str, *, refresh: str = "auto") -> RepoMap:
        repo_map = RepoMap(main_model=self.model, root=root, io=_FakeIO(), refresh=refresh)
        self._repo_maps.append(repo_map)
        return repo_map

    def init_git_repo(self, root: str) -> None:
        subprocess.run(["git", "init", root], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", root, "config", "user.name", "Test User"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", root, "config", "user.email", "test@example.com"],
            check=True,
            capture_output=True,
            text=True,
        )

    def commit_all(self, root: str, message: str) -> None:
        subprocess.run(["git", "-C", root, "add", "."], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", root, "commit", "-m", message],
            check=True,
            capture_output=True,
            text=True,
        )


@unittest.skipUnless(HAS_FULL_REPOMAP_DEPS, "optional repomap parser deps unavailable")
class TestRepoMap(_RepoMapTestCase):
    def test_get_repo_map(self):
        test_files = [
            "test_file1.py",
            "test_file2.py",
            "test_file3.md",
            "test_file4.json",
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            for file in test_files:
                Path(temp_dir, file).write_text("", encoding="utf-8")

            repo_map = self.make_repo_map(temp_dir)
            other_files = [os.path.join(temp_dir, file) for file in test_files]
            result = repo_map.get_repo_map([], other_files)

            self.assertIn("test_file1.py", result)
            self.assertIn("test_file2.py", result)
            self.assertIn("test_file3.md", result)
            self.assertIn("test_file4.json", result)

    def test_repo_map_refresh_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.init_git_repo(temp_dir)

            Path(temp_dir, "file1.py").write_text(
                "def function1():\n    return 'Hello from file1'\n",
                encoding="utf-8",
            )
            Path(temp_dir, "file2.py").write_text(
                "def function2():\n    return 'Hello from file2'\n",
                encoding="utf-8",
            )
            Path(temp_dir, "file3.py").write_text(
                "def function3():\n    return 'Hello from file3'\n",
                encoding="utf-8",
            )
            self.commit_all(temp_dir, "Initial commit")

            repo_map = self.make_repo_map(temp_dir, refresh="files")
            other_files = [
                os.path.join(temp_dir, "file1.py"),
                os.path.join(temp_dir, "file2.py"),
                os.path.join(temp_dir, "file3.py"),
            ]

            initial_map = repo_map.get_repo_map([], other_files)
            self.assertIn("function1", initial_map)
            self.assertIn("function2", initial_map)
            self.assertIn("function3", initial_map)

            with open(os.path.join(temp_dir, "file1.py"), "a", encoding="utf-8") as handle:
                handle.write("\ndef functionNEW():\n    return 'Hello NEW'\n")

            second_map = repo_map.get_repo_map([], other_files)
            self.assertEqual(initial_map, second_map)

            second_map = repo_map.get_repo_map([], other_files[:2])
            self.assertIn("functionNEW", second_map)

    def test_repo_map_refresh_auto(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.init_git_repo(temp_dir)
            Path(temp_dir, "file1.py").write_text(
                "def function1():\n    return 'Hello from file1'\n",
                encoding="utf-8",
            )
            Path(temp_dir, "file2.py").write_text(
                "def function2():\n    return 'Hello from file2'\n",
                encoding="utf-8",
            )
            self.commit_all(temp_dir, "Initial commit")

            repo_map = self.make_repo_map(temp_dir, refresh="auto")
            chat_files = []
            other_files = [os.path.join(temp_dir, "file1.py"), os.path.join(temp_dir, "file2.py")]

            original_get_ranked_tags = repo_map.get_ranked_tags

            def slow_get_ranked_tags(*args, **kwargs):
                time.sleep(1.1)
                return original_get_ranked_tags(*args, **kwargs)

            repo_map.get_ranked_tags = slow_get_ranked_tags

            initial_map = repo_map.get_repo_map(chat_files, other_files)
            self.assertIn("function1", initial_map)
            self.assertIn("function2", initial_map)
            self.assertNotIn("functionNEW", initial_map)

            with open(os.path.join(temp_dir, "file1.py"), "a", encoding="utf-8") as handle:
                handle.write("\ndef functionNEW():\n    return 'Hello NEW'\n")

            second_map = repo_map.get_repo_map(chat_files, other_files)
            self.assertEqual(initial_map, second_map)

            final_map = repo_map.get_repo_map(chat_files, other_files, force_refresh=True)
            self.assertIn("functionNEW", final_map)
            self.assertNotEqual(initial_map, final_map)

    def test_get_repo_map_with_identifiers(self):
        test_file1 = "test_file_with_identifiers.py"
        file_content1 = """\
class MyClass:
    def my_method(self, arg1, arg2):
        return arg1 + arg2

def my_function(arg1, arg2):
    return arg1 * arg2
"""

        test_file2 = "test_file_import.py"
        file_content2 = """\
from test_file_with_identifiers import MyClass

obj = MyClass()
print(obj.my_method(1, 2))
print(my_function(3, 4))
"""

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, test_file1).write_text(file_content1, encoding="utf-8")
            Path(temp_dir, test_file2).write_text(file_content2, encoding="utf-8")
            Path(temp_dir, "test_file_pass.py").write_text("pass", encoding="utf-8")

            repo_map = self.make_repo_map(temp_dir)
            other_files = [
                os.path.join(temp_dir, test_file1),
                os.path.join(temp_dir, test_file2),
                os.path.join(temp_dir, "test_file_pass.py"),
            ]
            result = repo_map.get_repo_map([], other_files)

            self.assertIn("test_file_with_identifiers.py", result)
            self.assertIn("MyClass", result)
            self.assertIn("my_method", result)
            self.assertIn("my_function", result)
            self.assertIn("test_file_pass.py", result)

    def test_get_repo_map_all_files(self):
        test_files = [
            "test_file0.py",
            "test_file1.txt",
            "test_file2.md",
            "test_file3.json",
            "test_file4.html",
            "test_file5.css",
            "test_file6.js",
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            for file in test_files:
                Path(temp_dir, file).write_text("", encoding="utf-8")

            repo_map = self.make_repo_map(temp_dir)
            other_files = [os.path.join(temp_dir, file) for file in test_files]
            result = repo_map.get_repo_map([], other_files)

            for file in test_files:
                self.assertIn(file, result)

    def test_get_repo_map_excludes_added_files(self):
        test_files = [
            "test_file1.py",
            "test_file2.py",
            "test_file3.md",
            "test_file4.json",
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            for file in test_files:
                Path(temp_dir, file).write_text("def foo(): pass\n", encoding="utf-8")

            repo_map = self.make_repo_map(temp_dir)
            test_paths = [os.path.join(temp_dir, file) for file in test_files]
            result = repo_map.get_repo_map(test_paths[:2], test_paths[2:])

            self.assertNotIn("test_file1.py", result)
            self.assertNotIn("test_file2.py", result)
            self.assertIn("test_file3.md", result)
            self.assertIn("test_file4.json", result)


@unittest.skipUnless(HAS_FULL_REPOMAP_DEPS, "optional repomap parser deps unavailable")
class TestRepoMapAllLanguages(_RepoMapTestCase):
    def setUp(self):
        super().setUp()
        self.fixtures_dir = Path(__file__).parent.parent / "fixtures" / "languages"

    def test_language_c(self):
        self._test_language_repo_map("c", "c", "main")

    def test_language_cpp(self):
        self._test_language_repo_map("cpp", "cpp", "main")

    def test_language_d(self):
        self._test_language_repo_map("d", "d", "main")

    def test_language_dart(self):
        self._test_language_repo_map("dart", "dart", "Person")

    def test_language_elixir(self):
        self._test_language_repo_map("elixir", "ex", "Greeter")

    def test_language_gleam(self):
        self._test_language_repo_map("gleam", "gleam", "greet")

    def test_language_haskell(self):
        self._test_language_repo_map("haskell", "hs", "add")

    def test_language_java(self):
        self._test_language_repo_map("java", "java", "Greeting")

    def test_language_javascript(self):
        self._test_language_repo_map("javascript", "js", "Person")

    def test_language_kotlin(self):
        self._test_language_repo_map("kotlin", "kt", "Greeting")

    def test_language_lua(self):
        self._test_language_repo_map("lua", "lua", "greet")

    def test_language_php(self):
        self._test_language_repo_map("php", "php", "greet")

    def test_language_python(self):
        self._test_language_repo_map("python", "py", "Person")

    def test_language_ruby(self):
        self._test_language_repo_map("ruby", "rb", "greet")

    def test_language_rust(self):
        self._test_language_repo_map("rust", "rs", "Person")

    def test_language_typescript(self):
        self._test_language_repo_map("typescript", "ts", "greet")

    def test_language_tsx(self):
        self._test_language_repo_map("tsx", "tsx", "UserProps")

    def test_language_zig(self):
        self._test_language_repo_map("zig", "zig", "add")

    def test_language_csharp(self):
        self._test_language_repo_map("csharp", "cs", "IGreeter")

    def test_language_elisp(self):
        self._test_language_repo_map("elisp", "el", "greeter")

    def test_language_elm(self):
        self._test_language_repo_map("elm", "elm", "Person")

    def test_language_go(self):
        self._test_language_repo_map("go", "go", "Greeter")

    def test_language_hcl(self):
        self._test_language_repo_map("hcl", "tf", "aws_vpc")

    def test_language_arduino(self):
        self._test_language_repo_map("arduino", "ino", "setup")

    def test_language_chatito(self):
        self._test_language_repo_map("chatito", "chatito", "intent")

    def test_language_clojure(self):
        self._test_language_repo_map("clojure", "clj", "greet")

    def test_language_commonlisp(self):
        self._test_language_repo_map("commonlisp", "lisp", "greet")

    def test_language_pony(self):
        self._test_language_repo_map("pony", "pony", "Greeter")

    def test_language_properties(self):
        self._test_language_repo_map("properties", "properties", "database.url")

    def test_language_r(self):
        self._test_language_repo_map("r", "r", "calculate")

    def test_language_racket(self):
        self._test_language_repo_map("racket", "rkt", "greet")

    def test_language_solidity(self):
        self._test_language_repo_map("solidity", "sol", "SimpleStorage")

    def test_language_swift(self):
        self._test_language_repo_map("swift", "swift", "Greeter")

    def test_language_udev(self):
        self._test_language_repo_map("udev", "rules", "USB_DRIVER")

    def test_language_scala(self):
        self._test_language_repo_map("scala", "scala", "Greeter")

    def test_language_ocaml(self):
        self._test_language_repo_map("ocaml", "ml", "Greeter")

    def test_language_ocaml_interface(self):
        self._test_language_repo_map("ocaml_interface", "mli", "Greeter")

    def test_language_matlab(self):
        self._test_language_repo_map("matlab", "m", "Person")

    def _test_language_repo_map(self, lang, key, symbol):
        fixture_dir = self.fixtures_dir / lang
        filename = f"test.{key}"
        fixture_path = fixture_dir / filename
        self.assertTrue(fixture_path.exists(), f"Fixture file missing for {lang}: {fixture_path}")

        content = fixture_path.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            test_file = os.path.join(temp_dir, filename)
            Path(test_file).write_text(content, encoding="utf-8")

            repo_map = self.make_repo_map(temp_dir)
            result = repo_map.get_repo_map([], [test_file])

            self.assertGreater(len(result.strip().splitlines()), 1)
            self.assertIn(filename, result)
            self.assertIn(symbol, result)

    def test_repo_map_sample_code_base(self):
        sample_code_base = Path(__file__).parent.parent / "fixtures" / "sample-code-base"
        expected_map_file = (
            Path(__file__).parent.parent / "fixtures" / "sample-code-base-repo-map.txt"
        )

        self.assertTrue(sample_code_base.exists(), "Sample code base directory not found")
        self.assertTrue(expected_map_file.exists(), "Expected repo map file not found")

        repomap_root = Path(__file__).parent.parent.parent
        repo_map = self.make_repo_map(str(repomap_root))
        other_files = [str(f) for f in sample_code_base.rglob("*") if f.is_file()]
        generated_map_str = repo_map.get_repo_map([], other_files).strip()
        expected_map = expected_map_file.read_text(encoding="utf-8").strip()

        if os.name == "nt":
            expected_map = re.sub(
                r"tests/fixtures/sample-code-base/([^:]+)",
                r"tests\\fixtures\\sample-code-base\\\1",
                expected_map,
            )
            generated_map_str = re.sub(
                r"tests/fixtures/sample-code-base/([^:]+)",
                r"tests\\fixtures\\sample-code-base\\\1",
                generated_map_str,
            )

        if generated_map_str != expected_map:
            diff = list(
                difflib.unified_diff(
                    expected_map.splitlines(),
                    generated_map_str.splitlines(),
                    fromfile="expected",
                    tofile="generated",
                    lineterm="",
                )
            )
            diff_text = "\n".join(diff)
            self.fail(f"Generated map differs from expected map:\n{diff_text}")

        self.assertEqual(generated_map_str, expected_map)


if __name__ == "__main__":
    unittest.main()
