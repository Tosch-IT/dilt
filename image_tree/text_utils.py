"""
Text normalizations, regex substitutions, and parsing commands.
"""
import re
import shlex

NORMALIZE_PATTERNS = [
    (re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}:\d+\b'), '<IP:PORT>'),
    (re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b'), '<IP>'),
    (re.compile(r'\b[a-fA-F0-9]{32,128}\b'), '<HASH>'),
    (re.compile(r'\b(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?|\d{4}[-/]\d{2}[-/]\d{2})|(?:\d{10,16})\b'), '<TIMESTAMP>'),
    (re.compile(r'\b(?:v\d+(?:\.\d+)*(?:-[a-zA-Z0-9]+)*|\d+\.\d+(?:\.\d+)*(?:-[a-zA-Z0-9]+)*)\b'), '<VERSION>'),
]

def normalize_command(command: str) -> str:
    res = command
    for p, repl in NORMALIZE_PATTERNS:
        res = p.sub(repl, res)
    return res

def parse_user_substitutions(raw_text: str) -> tuple[str, list[tuple[re.Pattern, str]], bool]:
    lines = raw_text.splitlines(keepends=True)
    patterns = []
    invalid = False
    new_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            new_lines.append(line)
            continue

        try:
            tokens = shlex.split(stripped)
            if len(tokens) != 2:
                raise ValueError("Line must contain exactly two quoted strings.")
            pat = re.compile(tokens[0])
            repl = tokens[1]
            patterns.append((pat, repl))
            new_lines.append(line)
        except Exception as e:
            invalid = True
            new_lines.append(f"# INVALID ({e}): {line.lstrip()}")

    return "".join(new_lines), patterns, invalid
