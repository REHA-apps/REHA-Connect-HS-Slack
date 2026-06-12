import re
from collections import defaultdict

ruff_file = (
    r"c:\Users\elaiy\Desktop\Integrations\crm-connectors\scratch\ruff_errors.txt"
)
pyright_file = (
    r"c:\Users\elaiy\Desktop\Integrations\crm-connectors\scratch\pyright_errors.txt"
)
out_file = r"C:\Users\elaiy\.gemini\antigravity-ide\brain\8e3d5f3d-e01d-4a69-b547-596e0dcf6df0\app_code_errors.md"


def read_file(filepath):
    try:
        with open(filepath, encoding="utf-16le") as f:
            return f.read()
    except:
        try:
            with open(filepath, encoding="utf-8") as f:
                return f.read()
        except:
            return ""


ruff_text = read_file(ruff_file)
pyright_text = read_file(pyright_file)

# Parse ruff errors
# Ruff format:
# E501 Line too long (89 > 88)
#   --> app\sqs_worker.py:70:89
ruff_errors_by_file = defaultdict(list)
ruff_error_counts = defaultdict(int)

# regex for finding ruff errors
# It's better to just extract all lines like "--> path:line:col" and the line before it.
ruff_lines = ruff_text.split("\n")
for i, line in enumerate(ruff_lines):
    if "-->" in line:
        match = re.search(r"-->\s*(.+?):(\d+):\d+", line)
        if match:
            filepath = match.group(1).strip()
            line_num = match.group(2)
            error_desc = ruff_lines[i - 1].strip() if i > 0 else "Unknown error"
            error_code_match = re.match(r"^([A-Z]+\d+)\s*(.*)", error_desc)
            if error_code_match:
                code = error_code_match.group(1)
                desc = error_code_match.group(2)
                ruff_error_counts[code] += 1
                ruff_errors_by_file[filepath].append(f"Line {line_num}: {code} {desc}")

# Parse pyright errors
# pyright format:
# c:\Users\elaiy\Desktop\Integrations\crm-connectors\app\utils\logger.py
#   C:\Users\elaiy\Desktop\Integrations\crm-connectors\app\utils\logger.py:12:12 - error: Expression of type "Any | None" cannot be assigned to return type "str"
pyright_errors_by_file = defaultdict(list)
pyright_lines = pyright_text.split("\n")
for line in pyright_lines:
    if " - error: " in line or " - warning: " in line:
        match = re.search(
            r"(app\\[^\:]+):(\d+):\d+\s+-\s+(error|warning):\s+(.*)", line
        )
        if not match:
            # try to find generic path
            match = re.search(
                r"([a-zA-Z0-9_\-\\]+\.py):(\d+):\d+\s+-\s+(error|warning):\s+(.*)", line
            )
        if match:
            filepath = match.group(1).strip()
            # simplify filepath to relative if absolute
            if "crm-connectors\\" in filepath:
                filepath = filepath.split("crm-connectors\\")[-1]
            line_num = match.group(2)
            level = match.group(3)
            msg = match.group(4)
            pyright_errors_by_file[filepath].append(f"Line {line_num} ({level}): {msg}")

with open(out_file, "w", encoding="utf-8") as out:
    out.write("# App Folder Code Analysis\n\n")

    out.write("## 1. Ruff Linting Errors Summary\n")
    out.write("| Error Code | Count | Description (example) |\n")
    out.write("|------------|-------|-----------------------|\n")
    for code, count in sorted(
        ruff_error_counts.items(), key=lambda x: x[1], reverse=True
    ):
        out.write(f"| {code} | {count} | |\n")

    out.write("\n### Details by File\n")
    for filepath, errors in sorted(ruff_errors_by_file.items()):
        out.write(f"\n**`{filepath}`** ({len(errors)} errors)\n")
        for err in errors[:5]:
            out.write(f"- {err}\n")
        if len(errors) > 5:
            out.write(f"- *... and {len(errors) - 5} more*\n")

    out.write("\n---\n\n## 2. Pyright Type Checking Errors Summary\n")
    total_pyright = sum(len(errs) for errs in pyright_errors_by_file.values())
    out.write(f"Total Pyright Issues: {total_pyright}\n\n")

    out.write("### Details by File\n")
    if total_pyright == 0:
        out.write("*No pyright errors found or failed to parse.*")
    else:
        for filepath, errors in sorted(pyright_errors_by_file.items()):
            out.write(f"\n**`{filepath}`** ({len(errors)} errors)\n")
            for err in errors[:5]:
                out.write(f"- {err}\n")
            if len(errors) > 5:
                out.write(f"- *... and {len(errors) - 5} more*\n")
