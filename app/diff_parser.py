"""Unified diff parsing: split into per-file blocks, extract added lines with
new-file line numbers. Mirrors the tested Node implementation exactly."""

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class RawFile:
    path: str
    raw_lines: list = field(default_factory=list)


@dataclass
class AddedLine:
    line: int
    text: str


@dataclass
class ParsedFile:
    path: str
    raw_text: str
    byte_length: int
    added_lines: list  # list[AddedLine]


def split_into_files(diff_text: str):
    lines = diff_text.split("\n")
    files = []
    current = None

    def start_new_file(path):
        nonlocal current
        current = RawFile(path=path or "unknown")
        files.append(current)

    for line in lines:
        if line.startswith("diff --git "):
            m = re.match(r"^diff --git a/(.+) b/(.+)$", line)
            path = m.group(2) if m else None
            start_new_file(path)
            current.raw_lines.append(line)
            continue
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path == "/dev/null":
                pass
            elif current is None or any(l.startswith("+++ ") for l in current.raw_lines):
                start_new_file(path)
            else:
                current.path = path
            if current is None:
                start_new_file(path)
            current.raw_lines.append(line)
            continue
        if line.startswith("--- ") and current is None:
            start_new_file(None)
            current.raw_lines.append(line)
            continue
        if current is None:
            if line.strip() == "":
                continue
            start_new_file(None)
        current.raw_lines.append(line)

    return [f for f in files if f.raw_lines]


def parse_file_hunks(raw_lines):
    added = []
    new_line_no = None
    saw_hunk = False

    for line in raw_lines:
        m = HUNK_RE.match(line)
        if m:
            new_line_no = int(m.group(1))
            saw_hunk = True
            continue
        if new_line_no is None:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(AddedLine(line=new_line_no, text=line[1:]))
            new_line_no += 1
        elif line.startswith("-"):
            pass
        elif line.startswith(" ") or line == "":
            new_line_no += 1
        elif line.startswith("\\"):
            pass
        else:
            new_line_no += 1

    return added, saw_hunk


@dataclass
class ParseResult:
    valid: bool
    files: list  # list[ParsedFile]


def parse_diff(diff_text: str) -> ParseResult:
    if not isinstance(diff_text, str) or diff_text.strip() == "":
        return ParseResult(valid=False, files=[])

    raw_files = split_into_files(diff_text)
    if not raw_files:
        return ParseResult(valid=False, files=[])

    files = []
    any_valid_hunk = False

    for rf in raw_files:
        added, saw_hunk = parse_file_hunks(rf.raw_lines)
        if saw_hunk:
            any_valid_hunk = True
        raw_text = "\n".join(rf.raw_lines)
        files.append(
            ParsedFile(
                path=rf.path,
                raw_text=raw_text,
                byte_length=len(raw_text.encode("utf-8")),
                added_lines=added,
            )
        )

    if not any_valid_hunk:
        return ParseResult(valid=False, files=[])

    return ParseResult(valid=True, files=files)
