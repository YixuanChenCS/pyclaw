import builtins
from pathlib import Path

from pyclaw.help import Help, SimpleRetriever


def test_help_uses_simple_retriever_when_llama_index_is_unavailable(tmp_path, monkeypatch):
    docs_dir = tmp_path / "website" / "docs"
    docs_dir.mkdir(parents=True)

    usage_doc = docs_dir / "usage.md"
    usage_doc.write_text("# Usage\nPyclaw browser usage and chat workflow.\n", encoding="utf-8")

    faq_doc = docs_dir / "faq.md"
    faq_doc.write_text("# FAQ\nPyclaw can answer AI coding questions.\n", encoding="utf-8")

    monkeypatch.setattr("pyclaw.help.get_package_files", lambda: [usage_doc, faq_doc])
    monkeypatch.setattr("pyclaw.help.exclude_website_pats", [])

    original_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("llama_index"):
            raise ImportError("forced missing optional dependency")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    help_instance = Help()

    assert isinstance(help_instance.retriever, SimpleRetriever)

    result = help_instance.ask("browser usage")
    assert "# Question: browser usage" in result
    assert 'from_url="https://pyclaw.chat/docs/usage.html"' in result
    assert "Pyclaw browser usage" in result
