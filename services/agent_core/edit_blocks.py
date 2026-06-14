from __future__ import annotations

from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
import re

from diff_match_patch import diff_match_patch


@dataclass(frozen=True, slots=True)
class SearchReplaceEdit:
    path: str
    search: str
    replace: str


class SearchReplaceApplicationError(ValueError):
    def __init__(self, failure_code: str, message: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code


def strip_quoted_wrapping(text: str, path: str | None = None, fence: tuple[str, str] = ("```", "```")) -> str:
    if not text:
        return text

    lines = text.splitlines()
    if path and lines and lines[0].strip().endswith(path.split("/")[-1]):
        lines = lines[1:]
    if lines and lines[0].startswith(fence[0]) and lines[-1].startswith(fence[1]):
        lines = lines[1:-1]

    rendered = "\n".join(lines)
    if rendered and not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def apply_search_replace_edits(
    original_contents: dict[str, str],
    edits: tuple[SearchReplaceEdit, ...],
) -> dict[str, str]:
    updated_contents = dict(original_contents)
    for edit in edits:
        content = updated_contents.get(edit.path)
        if content is None:
            raise SearchReplaceApplicationError(
                "missing_source_content",
                f"Missing source content for target file {edit.path!r}",
            )
        updated = do_replace(edit.path, content, edit.search, edit.replace)
        updated_contents[edit.path] = updated
    return updated_contents


def do_replace(path: str, content: str, before_text: str, after_text: str) -> str:
    before_text = strip_quoted_wrapping(before_text, path)
    after_text = strip_quoted_wrapping(after_text, path)

    if not before_text.strip():
        raise SearchReplaceApplicationError(
            "schema_invalid",
            f"SEARCH block for {path!r} must not be empty",
        )

    return _replace_exactly_once(path, content, before_text, after_text)


def _replace_exactly_once(path: str, content: str, before_text: str, after_text: str) -> str:
    match_count = content.count(before_text)
    if match_count == 0:
        raise SearchReplaceApplicationError(
            "search_not_found",
            f"SEARCH block did not match the current content of {path!r}",
        )
    if match_count > 1:
        raise SearchReplaceApplicationError(
            "ambiguous_search",
            f"SEARCH block matched multiple locations in {path!r}",
        )
    return content.replace(before_text, after_text, 1)


def replace_most_similar_chunk(whole: str, part: str, replace: str) -> str | None:
    whole, whole_lines = _prep(whole)
    part, part_lines = _prep(part)
    replace, replace_lines = _prep(replace)

    result = _perfect_or_whitespace(whole_lines, part_lines, replace_lines)
    if result is not None:
        return result

    if len(part_lines) > 2 and not part_lines[0].strip():
        result = _perfect_or_whitespace(whole_lines, part_lines[1:], replace_lines)
        if result is not None:
            return result

    try:
        result = _try_dotdotdots(whole, part, replace)
    except ValueError:
        result = None
    if result is not None:
        return result

    result = _flexible_search_and_replace((part, replace, whole))
    if result is not None:
        return result

    return None


def build_unified_diff(
    *,
    path: str,
    before: str,
    after: str,
) -> str:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    rendered_lines: list[str] = []
    for line in unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    ):
        rendered_lines.append(line if line.endswith("\n") else f"{line}\n")
    return "".join(rendered_lines)


def _prep(content: str) -> tuple[str, list[str]]:
    if content and not content.endswith("\n"):
        content += "\n"
    return content, content.splitlines(keepends=True)


def _perfect_or_whitespace(
    whole_lines: list[str],
    part_lines: list[str],
    replace_lines: list[str],
) -> str | None:
    result = _perfect_replace(whole_lines, part_lines, replace_lines)
    if result is not None:
        return result
    return _replace_part_with_missing_leading_whitespace(whole_lines, part_lines, replace_lines)


def _perfect_replace(
    whole_lines: list[str],
    part_lines: list[str],
    replace_lines: list[str],
) -> str | None:
    part_tuple = tuple(part_lines)
    part_len = len(part_lines)
    for index in range(len(whole_lines) - part_len + 1):
        if tuple(whole_lines[index : index + part_len]) == part_tuple:
            return "".join(whole_lines[:index] + replace_lines + whole_lines[index + part_len :])
    return None


def _replace_part_with_missing_leading_whitespace(
    whole_lines: list[str],
    part_lines: list[str],
    replace_lines: list[str],
) -> str | None:
    leading = [len(line) - len(line.lstrip()) for line in part_lines if line.strip()] + [
        len(line) - len(line.lstrip()) for line in replace_lines if line.strip()
    ]
    if leading and min(leading):
        trim = min(leading)
        part_lines = [line[trim:] if line.strip() else line for line in part_lines]
        replace_lines = [line[trim:] if line.strip() else line for line in replace_lines]

    part_len = len(part_lines)
    for index in range(len(whole_lines) - part_len + 1):
        indent = _match_but_for_leading_whitespace(
            whole_lines[index : index + part_len],
            part_lines,
        )
        if indent is None:
            continue
        adjusted_replace = [
            indent + line if line.strip() else line
            for line in replace_lines
        ]
        return "".join(whole_lines[:index] + adjusted_replace + whole_lines[index + part_len :])
    return None


def _match_but_for_leading_whitespace(
    whole_lines: list[str],
    part_lines: list[str],
) -> str | None:
    if not all(
        whole_lines[index].lstrip() == part_lines[index].lstrip()
        for index in range(len(whole_lines))
    ):
        return None

    prefixes = {
        whole_lines[index][: len(whole_lines[index]) - len(part_lines[index])]
        for index in range(len(whole_lines))
        if whole_lines[index].strip()
    }
    if len(prefixes) != 1:
        return None
    return prefixes.pop()


def _try_dotdotdots(whole: str, part: str, replace: str) -> str | None:
    dots_re = re.compile(r"(^\s*\.\.\.\n)", re.MULTILINE | re.DOTALL)

    part_pieces = re.split(dots_re, part)
    replace_pieces = re.split(dots_re, replace)

    if len(part_pieces) != len(replace_pieces):
        raise ValueError("Unpaired ... in SEARCH/REPLACE block")
    if len(part_pieces) == 1:
        return None

    all_dots_match = all(
        part_pieces[index] == replace_pieces[index]
        for index in range(1, len(part_pieces), 2)
    )
    if not all_dots_match:
        raise ValueError("Unmatched ... in SEARCH/REPLACE block")

    part_chunks = [part_pieces[index] for index in range(0, len(part_pieces), 2)]
    replace_chunks = [replace_pieces[index] for index in range(0, len(replace_pieces), 2)]

    updated = whole
    for search_chunk, replace_chunk in zip(part_chunks, replace_chunks):
        if not search_chunk and not replace_chunk:
            continue
        if not search_chunk and replace_chunk:
            if not updated.endswith("\n"):
                updated += "\n"
            updated += replace_chunk
            continue
        if updated.count(search_chunk) != 1:
            raise ValueError("SEARCH/REPLACE block with ... must match exactly once")
        updated = updated.replace(search_chunk, replace_chunk, 1)
    return updated


class _RelativeIndenter:
    def __init__(self, texts: tuple[str, str, str]) -> None:
        chars = set().union(*texts)
        marker = "←"
        if marker not in chars:
            self.marker = marker
            return
        for codepoint in range(0x10FFFF, 0x10000, -1):
            candidate = chr(codepoint)
            if candidate not in chars:
                self.marker = candidate
                return
        raise ValueError("Could not find a unique relative-indent marker")

    def make_relative(self, text: str) -> str:
        if self.marker in text:
            raise ValueError(f"Text already contains the outdent marker: {self.marker}")
        lines = text.splitlines(keepends=True)
        output: list[str] = []
        prev_indent = ""
        for line in lines:
            line_without_end = line.rstrip("\n\r")
            indent_len = len(line_without_end) - len(line_without_end.lstrip())
            indent = line[:indent_len]
            change = indent_len - len(prev_indent)
            if change > 0:
                current_indent = indent[-change:]
            elif change < 0:
                current_indent = self.marker * -change
            else:
                current_indent = ""
            output.append(current_indent + "\n" + line[indent_len:])
            prev_indent = indent
        return "".join(output)

    def make_absolute(self, text: str) -> str:
        lines = text.splitlines(keepends=True)
        output: list[str] = []
        prev_indent = ""
        for index in range(0, len(lines), 2):
            indent_delta = lines[index].rstrip("\r\n")
            body = lines[index + 1]
            if indent_delta.startswith(self.marker):
                current_indent = prev_indent[: -len(indent_delta)]
            else:
                current_indent = prev_indent + indent_delta
            output.append(body if not body.rstrip("\r\n") else current_indent + body)
            prev_indent = current_indent
        rendered = "".join(output)
        if self.marker in rendered:
            raise ValueError("Relative-indent restoration left marker characters in output")
        return rendered


def _flexible_search_and_replace(texts: tuple[str, str, str]) -> str | None:
    strategies = (_search_and_replace, _dmp_lines_apply)
    preprocessors = (
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    )
    for strategy in strategies:
        for strip_blank_lines, relative_indent in preprocessors:
            result = _try_strategy(
                texts,
                strategy,
                strip_blank_lines=strip_blank_lines,
                relative_indent=relative_indent,
            )
            if result is not None:
                return result
    return None


def _try_strategy(
    texts: tuple[str, str, str],
    strategy,
    *,
    strip_blank_lines: bool,
    relative_indent: bool,
) -> str | None:
    working = texts
    indenter: _RelativeIndenter | None = None

    if strip_blank_lines:
        working = tuple(text.strip("\n") + "\n" for text in working)
    if relative_indent:
        indenter = _RelativeIndenter(working)
        working = tuple(indenter.make_relative(text) for text in working)

    result = strategy(working)
    if result is None:
        return None
    if indenter is not None:
        try:
            result = indenter.make_absolute(result)
        except ValueError:
            return None
    return result


def _search_and_replace(texts: tuple[str, str, str]) -> str | None:
    search_text, replace_text, original_text = texts
    match_count = original_text.count(search_text)
    if match_count != 1:
        return None
    return original_text.replace(search_text, replace_text, 1)


def _dmp_lines_apply(texts: tuple[str, str, str]) -> str | None:
    search_text, replace_text, original_text = texts
    for text in texts:
        if not text.endswith("\n"):
            return None

    dmp = diff_match_patch()
    dmp.Diff_Timeout = 5
    dmp.Match_Threshold = 0.1
    dmp.Match_Distance = 100_000
    dmp.Match_MaxBits = 32
    dmp.Patch_Margin = 1

    combined = search_text + replace_text + original_text
    all_lines, _, mapping = dmp.diff_linesToChars(combined, "")

    search_count = len(search_text.splitlines())
    replace_count = len(replace_text.splitlines())
    original_count = len(original_text.splitlines())

    search_lines = all_lines[:search_count]
    replace_lines = all_lines[search_count : search_count + replace_count]
    original_lines = all_lines[search_count + replace_count : search_count + replace_count + original_count]

    diff_lines = dmp.diff_main(search_lines, replace_lines, None)
    dmp.diff_cleanupSemantic(diff_lines)
    dmp.diff_cleanupEfficiency(diff_lines)
    patches = dmp.patch_make(search_lines, diff_lines)
    new_lines, success = dmp.patch_apply(patches, original_lines)
    if False in success:
        return None
    return "".join(mapping[ord(char)] for char in new_lines)
