import os
import re
from collections import defaultdict


def normalize_line(line):
    # Strip comments starting with #
    # (Note: does not handle inline comments inside strings perfectly, but sufficient for duplicate detection)
    line = re.sub(r"#.*$", "", line)
    line = line.strip()
    # Normalize quotes
    line = line.replace("'", '"')
    # Normalize internal whitespace
    line = re.sub(r"\s+", " ", line)
    return line


def scan_files(directory):
    file_lines = {}
    for root, _, files in os.walk(directory):
        # Skip __pycache__ and env directories
        if "__pycache__" in root or ".venv" in root or ".git" in root:
            continue
        for file in files:
            if file.endswith(".py"):
                file_path = os.path.join(root, file)
                normalized_path = os.path.relpath(file_path, directory)

                try:
                    with open(file_path, encoding="utf-8") as f:
                        raw_lines = f.readlines()
                except Exception as e:
                    print(f"Error reading {file_path}: {e}")
                    continue

                processed = []
                for idx, raw in enumerate(raw_lines):
                    line_num = idx + 1
                    norm = normalize_line(raw)
                    if norm:  # skip empty lines and comments
                        processed.append((line_num, raw, norm))

                file_lines[normalized_path] = processed
    return file_lines


def find_duplicates(file_lines, min_lines=8):
    # Slide window of size min_lines over each file
    windows = defaultdict(list)
    for path, lines in file_lines.items():
        if len(lines) < min_lines:
            continue
        for i in range(len(lines) - min_lines + 1):
            window = lines[i : i + min_lines]
            normalized_tuple = tuple(item[2] for item in window)
            start_raw_line = window[0][0]
            end_raw_line = window[-1][0]
            windows[normalized_tuple].append((path, start_raw_line, end_raw_line, i))

    # Keep only windows with duplicates (occurring in multiple places)
    duplicates = {k: v for k, v in windows.items() if len(v) > 1}

    # We want to merge consecutive windows.
    # A match is defined by a set of locations.
    # Let's group duplicate matches by pairs of files/locations to merge them.
    # Pair key: ((file1, index1), (file2, index2))
    # We can do a pairwise merge.
    merged_matches = []

    # To keep it simple: Let's find all pairs of (loc1, loc2) that match.
    # If loc1_index + 1 and loc2_index + 1 also match, we can extend the current match.
    # Let's build all matching pairs of windows.
    pairs = []
    for norm_seq, locs in duplicates.items():
        for i in range(len(locs)):
            for j in range(i + 1, len(locs)):
                loc1 = locs[i]
                loc2 = locs[j]
                pairs.append((loc1, loc2, norm_seq))

    # Sort pairs by file1, file2, and start index of file1 to make merging easier
    pairs.sort(key=lambda x: (x[0][0], x[1][0], x[0][3], x[1][3]))

    active_matches = []

    for loc1, loc2, seq in pairs:
        file1, s1, e1, idx1 = loc1
        file2, s2, e2, idx2 = loc2

        # Try to extend an existing active match
        extended = False
        for match in active_matches:
            # Match is: {file1, file2, start1, end1, start2, end2, last_idx1, last_idx2}
            # We check if this pair is a direct continuation of an active match
            if (
                match["file1"] == file1
                and match["file2"] == file2
                and idx1 == match["last_idx1"] + 1
                and idx2 == match["last_idx2"] + 1
            ):
                # Extend it
                match["end1"] = e1
                match["end2"] = e2
                match["last_idx1"] = idx1
                match["last_idx2"] = idx2
                match["length"] += 1
                extended = True
                break

        if not extended:
            active_matches.append(
                {
                    "file1": file1,
                    "file2": file2,
                    "start1": s1,
                    "end1": e1,
                    "start2": s2,
                    "end2": e2,
                    "last_idx1": idx1,
                    "last_idx2": idx2,
                    "length": min_lines,
                }
            )

    # Filter active matches to remove sub-matches that were fully absorbed,
    # or just sort and group them nicely.
    # Note: When we merge, some matches might overlap or be duplicates. Let's clean them up.
    # We want to keep only the maximal matches.
    # A match A is sub-match of B if A's range is subset of B's range for both files.
    maximal_matches = []
    for m in sorted(active_matches, key=lambda x: x["length"], reverse=True):
        is_sub = False
        for max_m in maximal_matches:
            if m["file1"] == max_m["file1"] and m["file2"] == max_m["file2"]:
                # check subset
                # Note: original line numbers start1, end1, start2, end2
                if (
                    max_m["start1"] <= m["start1"]
                    and m["end1"] <= max_m["end1"]
                    and max_m["start2"] <= m["start2"]
                    and m["end2"] <= max_m["end2"]
                ):
                    is_sub = True
                    break
        if not is_sub:
            maximal_matches.append(m)

    # Sort maximal matches by file1, then start1, then length
    maximal_matches.sort(key=lambda x: (x["file1"], x["start1"], -x["length"]))
    return maximal_matches


def main():
    workspace_dir = r"c:\Users\elaiy\Desktop\Integrations\crm-connectors\app"
    print(f"Scanning directory: {workspace_dir}")
    file_lines = scan_files(workspace_dir)
    print(f"Scanned {len(file_lines)} python files.")

    # Increase min_lines to 12 to find larger, more meaningful duplicated blocks
    min_lines = 12
    matches = find_duplicates(file_lines, min_lines=min_lines)

    output_path = r"c:\Users\elaiy\Desktop\Integrations\crm-connectors\scratch\duplicate_results.txt"
    with open(output_path, "w", encoding="utf-8") as out:
        out.write(
            f"Found {len(matches)} duplicated blocks of code (min {min_lines} lines):\n\n"
        )
        for idx, m in enumerate(matches, 1):
            out.write(f"Match #{idx}:\n")
            out.write(f"  - File A: {m['file1']} (Lines {m['start1']}-{m['end1']})\n")
            out.write(f"  - File B: {m['file2']} (Lines {m['start2']}-{m['end2']})\n")
            out.write(f"  - Length: {m['length']} non-empty lines\n\n")

    print(f"Wrote {len(matches)} matches to {output_path}")


if __name__ == "__main__":
    main()
