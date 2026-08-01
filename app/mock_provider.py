"""Deterministic mock provider: MOCK-001..008 and MOCK-INJ rules."""

import re

CREDENTIAL_RE = re.compile(
    r"(api[_-]?key|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]", re.IGNORECASE
)
SQL_KEYWORDS_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b")
NULL_CMP_RE = re.compile(r"(?<![=!])==(?!=)\s*null|!=(?!=)\s*null")
INJECTION_RE = re.compile(
    r"ignore previous instructions|disregard all prior|you are now", re.IGNORECASE
)
CATCH_RE = re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}")

SINGLE_LINE_RULES = [
    {
        "ruleId": "MOCK-001",
        "severity": "critical",
        "category": "security",
        "title": "eval usage",
        "test": lambda text: "eval(" in text,
    },
    {
        "ruleId": "MOCK-002",
        "severity": "critical",
        "category": "security",
        "title": "hardcoded credential",
        "test": lambda text: bool(CREDENTIAL_RE.search(text)),
    },
    {
        "ruleId": "MOCK-003",
        "severity": "high",
        "category": "security",
        "title": "SQL string concatenation",
        "test": lambda text: bool(SQL_KEYWORDS_RE.search(text))
        and "+" in text
        and bool(re.search(r"['\"]", text)),
    },
    {
        "ruleId": "MOCK-005",
        "severity": "medium",
        "category": "correctness",
        "title": "loose null comparison",
        "test": lambda text: bool(NULL_CMP_RE.search(text)),
    },
    {
        "ruleId": "MOCK-006",
        "severity": "medium",
        "category": "performance",
        "title": "deep-clone via JSON",
        "test": lambda text: "JSON.parse(JSON.stringify(" in text,
    },
    {
        "ruleId": "MOCK-007",
        "severity": "low",
        "category": "style",
        "title": "console.log left in",
        "test": lambda text: "console.log(" in text,
    },
    {
        "ruleId": "MOCK-008",
        "severity": "low",
        "category": "style",
        "title": "unresolved marker",
        "test": lambda text: "TODO" in text or "FIXME" in text,
    },
    {
        "ruleId": "MOCK-INJ",
        "severity": "critical",
        "category": "security",
        "title": "prompt-injection content",
        "test": lambda text: bool(INJECTION_RE.search(text)),
    },
]


def find_empty_catch_blocks(added_lines):
    """MOCK-004 can span multiple added lines; scan the joined text per file."""
    findings = []
    joined = ""
    offset_to_line = []
    for a in added_lines:
        offset_to_line.append((len(joined), a.line))
        joined += a.text + "\n"

    for m in CATCH_RE.finditer(joined):
        idx = m.start()
        matched_line = None
        for start, line in offset_to_line:
            if start <= idx:
                matched_line = line
            else:
                break
        if matched_line is not None:
            findings.append(matched_line)
    return findings


def scan_file(added_lines):
    findings = []
    seen = set()

    for a in added_lines:
        for rule in SINGLE_LINE_RULES:
            if rule["test"](a.text):
                key = (rule["ruleId"], a.line)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    {
                        "ruleId": rule["ruleId"],
                        "severity": rule["severity"],
                        "category": rule["category"],
                        "title": rule["title"],
                        "line": a.line,
                        "evidence": a.text,
                    }
                )

    by_line = {a.line: a.text for a in added_lines}
    for line in find_empty_catch_blocks(added_lines):
        key = ("MOCK-004", line)
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            {
                "ruleId": "MOCK-004",
                "severity": "high",
                "category": "correctness",
                "title": "swallowed exception",
                "line": line,
                "evidence": by_line.get(line, ""),
            }
        )

    return findings


def run_mock_provider(files):
    all_findings = []
    for f in files:
        for finding in scan_file(f.added_lines):
            all_findings.append(
                {
                    "id": f"{finding['ruleId']}:{f.path}:{finding['line']}",
                    "ruleId": finding["ruleId"],
                    "path": f.path,
                    "line": finding["line"],
                    "severity": finding["severity"],
                    "category": finding["category"],
                    "title": finding["title"],
                    "evidence": finding["evidence"],
                }
            )

    by_id = {f["id"]: f for f in all_findings}
    deduped = list(by_id.values())
    deduped.sort(key=lambda f: (f["path"], f["line"], f["ruleId"]))
    return deduped
