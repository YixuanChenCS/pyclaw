import os

from pyclaw.utils import safe_abs_path, split_chat_history_markdown


def test_safe_abs_path_symlink_loop(tmp_path):
    # Create circular symlink: a -> b -> a
    link_a = tmp_path / "link_a"
    link_b = tmp_path / "link_b"
    link_a.symlink_to(link_b)
    link_b.symlink_to(link_a)

    # safe_abs_path must not raise, and must return an absolute path
    result = safe_abs_path(str(link_a))
    assert os.path.isabs(result)


def test_split_chat_history_markdown_extracts_user_assistant_and_tool_messages():
    # Verifies the markdown splitter assigns `####` to user turns, `>` lines to tool turns, and plain text to assistant turns.
    # This catches parser regressions where tool output or user prompts would be merged into the wrong role.
    # The expected roles and contents are correct because the fixture text follows the splitter's documented prefixes.
    text = (
        "# title\n"
        "#### /add foo.py\n"
        "> Added foo.py to the chat\n"
        "Assistant reply line 1\n"
        "Assistant reply line 2\n"
    )

    messages = split_chat_history_markdown(text, include_tool=True)

    assert messages == [
        {"role": "user", "content": "/add foo.py\n"},
        {"role": "tool", "content": "Added foo.py to the chat\n"},
        {"role": "assistant", "content": "Assistant reply line 1\nAssistant reply line 2\n"},
    ]


def test_split_chat_history_markdown_excludes_tool_messages_by_default():
    # Verifies tool messages are filtered out unless explicitly requested.
    # This catches regressions where raw tool output would leak into normal chat history reconstruction.
    # The expected output is correct because include_tool defaults to False and only user/assistant turns remain.
    text = "#### /status\n> queued\nAssistant summary\n"

    messages = split_chat_history_markdown(text)

    assert messages == [
        {"role": "user", "content": "/status\n"},
        {"role": "assistant", "content": "Assistant summary\n"},
    ]
