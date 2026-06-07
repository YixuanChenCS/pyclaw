from pyclaw.commands import Commands, parse_quoted_filenames


def test_parse_quoted_filenames_handles_mixed_spacing():
    args = 'foo.py "dir with spaces/bar.py" baz.txt'
    assert parse_quoted_filenames(args) == ["foo.py", "dir with spaces/bar.py", "baz.txt"]


def test_quote_fname_only_wraps_paths_with_spaces():
    commands = Commands(None, None)
    assert commands.quote_fname("foo.py") == "foo.py"
    assert commands.quote_fname("dir with spaces/foo.py") == '"dir with spaces/foo.py"'


def test_is_command_recognizes_slash_and_bang_prefixes():
    commands = Commands(None, None)
    assert commands.is_command("/add foo.py") is True
    assert commands.is_command("!pytest -q") is True
    assert commands.is_command("plain text") is False
