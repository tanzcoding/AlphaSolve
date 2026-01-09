from typing import List, Optional, Tuple

def extract_substring(target_string, begin_str, end_str, *, logger=None, module="utils"):
    """Extract the substring between two markers from an LLM response."""
    if target_string is None:
        return None

    begin_index = target_string.find(begin_str)
    end_index = target_string.find(end_str)

    if begin_index < 0 or end_index < 0 or begin_index + len(begin_str) > end_index:
        message = (
            f"illegal response missing '{begin_str}' or '{end_str}' "
            f"(begin_index={begin_index}, end_index={end_index})"
        )
        if logger is not None:
            logger.log_print(message, module=module, level='ERROR')
        return None

    begin_index += len(begin_str)
    return target_string[begin_index:end_index]


def apply_unified_diff(original_text: str, diff_text: str) -> str:
    """
    Apply a unified diff to the original text using search-and-replace semantics.
    
    This function follows the best practices from aider's unified diff editing:
    - Line numbers in @@ headers are IGNORED (GPT is bad at them)
    - Each hunk is treated as a search-and-replace operation
    - Search for lines marked with '-' (deleted lines)
    - Replace with lines marked with '+' (added lines)
    - Context lines (marked with space) must match exactly
    - Flexible matching to handle common LLM mistakes
    
    Args:
        original_text: The original text to apply the diff to
        diff_text: The unified diff format text (line numbers in @@ headers are optional)

    Returns:
        The modified text after applying the diff

    Raises:
        ValueError: If the diff cannot be applied or is in invalid format
    """
    import re

    if original_text is None:
        original_text = ""

    if diff_text is None or not diff_text.strip():
        return original_text
    
    # Check for common mistakes: diff wrapped in XML tags
    if '<proof>' in diff_text or '<conjecture>' in diff_text:
        raise ValueError(
            "Invalid diff format: detected XML tags like <proof> or <conjecture>. "
            "You must use unified diff format with lines starting with '+' (add) or '-' (delete), "
            "NOT full text replacement wrapped in XML tags."
        )

    original_lines = original_text.split('\n')
    diff_lines = diff_text.split('\n')

    header_pattern = re.compile(r'^@@(?: [^@]+)?@@$')

    # Parse hunks from diff (ignore specific line numbers)
    hunks = []
    current_hunk = None
    in_hunk = False
    has_diff_markers = False

    for raw_line in diff_lines:
        line = raw_line.rstrip('\r')
        stripped = line.strip()

        if header_pattern.match(stripped):
            if current_hunk is not None:
                hunks.append(current_hunk)
            current_hunk = {'lines': []}
            in_hunk = True
            continue

        if line.startswith('---') or line.startswith('+++'):
            # File headers - skip
            continue

        if not in_hunk:
            if line.startswith(('+', '-', ' ')) or stripped.startswith('@@'):
                current_hunk = {'lines': []}
                in_hunk = True
            else:
                # Ignore stray lines before first hunk
                continue

        if current_hunk is None:
            current_hunk = {'lines': []}

        # Check for hunk header (@@ ... @@) BEFORE checking for space marker
        if stripped.startswith('@@'):
            if current_hunk['lines']:
                hunks.append(current_hunk)
            current_hunk = {'lines': []}
            continue

        marker = line[:1]
        payload = line[1:] if marker in ('+', '-', ' ') else line

        if marker == '+' and not line.startswith('+++'):
            current_hunk['lines'].append(('add', payload))
            has_diff_markers = True
        elif marker == '-' and not line.startswith('---'):
            current_hunk['lines'].append(('del', payload))
            has_diff_markers = True
        elif marker == ' ':
            current_hunk['lines'].append(('ctx', payload))
        else:
            # Heuristic: treat prefix-less lines inside a hunk as additions (LLM forgot '+')
            current_hunk['lines'].append(('add', line))
            has_diff_markers = True

    if current_hunk is not None and current_hunk.get('lines'):
        hunks.append(current_hunk)

    if not has_diff_markers:
        raise ValueError(
            "Invalid diff format: no lines starting with '+' or '-' found. "
            "Unified diff must contain lines starting with '+' (additions) or '-' (deletions). "
            "Make sure you're using proper diff syntax, not plain text."
        )

    def _leading_ws(line: str) -> int:
        stripped_line = line.lstrip('\t ')
        return len(line) - len(stripped_line)

    def _match_block(lines: List[str], target: List[str]) -> Optional[Tuple[int, int]]:
        if not target:
            return None
        limit = len(lines) - len(target) + 1
        for i in range(max(limit, 0)):
            if lines[i:i + len(target)] == target:
                return i, i + len(target)
        return None

    def _match_trimmed(lines: List[str], target: List[str]) -> Optional[Tuple[int, int]]:
        trimmed_target = [t.rstrip() for t in target]
        limit = len(lines) - len(target) + 1
        for i in range(max(limit, 0)):
            candidate = [ln.rstrip() for ln in lines[i:i + len(target)]]
            if candidate == trimmed_target:
                return i, i + len(target)
        return None

    def _match_relative_indent(lines: List[str], target: List[str]) -> Optional[Tuple[int, int]]:
        limit = len(lines) - len(target) + 1
        target_indents = [_leading_ws(t) for t in target]
        target_stripped = [t.lstrip('\t ') for t in target]
        for i in range(max(limit, 0)):
            candidate = lines[i:i + len(target)]
            indent_delta = None
            for cand_line, tgt_line, tgt_indent in zip(candidate, target_stripped, target_indents):
                cand_strip = cand_line.lstrip('\t ')
                if tgt_line.strip() == '' and cand_strip.strip() == '':
                    continue
                if cand_strip != tgt_line:
                    break
                cand_indent = _leading_ws(cand_line)
                delta = cand_indent - tgt_indent
                if indent_delta is None:
                    indent_delta = delta
                elif delta != indent_delta:
                    break
            else:
                return i, i + len(target)
        return None

    def _match_fuzzy(lines: List[str], target: List[str]) -> Optional[Tuple[int, int]]:
        limit = len(lines) - len(target) + 1
        best_match = None
        best_score = 0.0
        for i in range(max(limit, 0)):
            score = 0.0
            for j, tgt in enumerate(target):
                cand = lines[i + j]
                if cand == tgt:
                    score += 1.0
                elif cand.rstrip() == tgt.rstrip():
                    score += 0.75
                elif cand.strip() == tgt.strip():
                    score += 0.6
                elif tgt in cand or cand in tgt:
                    score += 0.4
            normalized = score / max(len(target), 1)
            if normalized > 0.6 and normalized > best_score:
                best_score = normalized
                best_match = (i, i + len(target))
        return best_match

    def _apply_replacement(lines: List[str], start_end: Tuple[int, int], replacement: List[str]) -> List[str]:
        start, end = start_end
        return lines[:start] + replacement + lines[end:]

    result_lines = original_lines.copy()

    for hunk in hunks:
        current_text = '\n'.join(result_lines)
        normalized_lines = []
        reclassified_diff = False
        for line_type, content in hunk['lines']:
            if line_type == 'ctx' and content.strip() and content not in current_text:
                normalized_lines.append(('add', content))
                reclassified_diff = True
            else:
                normalized_lines.append((line_type, content))
                if line_type in ('add', 'del'):
                    reclassified_diff = True

        if reclassified_diff:
            has_diff_markers = True

        search_lines: List[str] = []
        replace_lines: List[str] = []

        for line_type, content in normalized_lines:
            if line_type in ('del', 'ctx'):
                search_lines.append(content)
            if line_type in ('add', 'ctx'):
                replace_lines.append(content)

        if not search_lines:
            # Pure insertion hunk – append to end as fallback
            if replace_lines:
                result_lines.extend(replace_lines)
            continue

        match = _match_block(result_lines, search_lines)
        if match is None:
            match = _match_trimmed(result_lines, search_lines)
        if match is None:
            match = _match_relative_indent(result_lines, search_lines)
        if match is None:
            match = _match_fuzzy(result_lines, search_lines)

        if match is None:
            raise ValueError(
                "Failed to locate hunk target while applying diff. "
                "Ensure context lines match the current content."
            )

        result_lines = _apply_replacement(result_lines, match, replace_lines)

    return '\n'.join(result_lines)


def load_prompt_from_file(prompt_file_path):
    with open(prompt_file_path, 'r', encoding='utf-8') as f:
        return f.read()


def search_and_replace(text: str, operation: str) -> str:
    """Apply a SEARCH/REPLACE diff block to ``text`` in a diff-like fashion.

    The expected format is::

        <<<<<<< SEARCH
        original snippet (must match exactly)
        =======
        replacement snippet
        >>>>>>> REPLACE

    A shorthand of the form ``BEGIN_MARKER ... END_MARKER`` may be used inside
    the SEARCH section to capture multi-line spans. Both markers are included in
    the matched substring, so the replacement will remove them unless it also
    reintroduces them.
    """

    if operation is None or not operation.strip():
        raise ValueError("SEARCH/REPLACE operation must be a non-empty string")

    import textwrap

    search_marker = "<<<<<<< SEARCH"
    split_marker = "======="
    replace_marker = ">>>>>>> REPLACE"

    if search_marker not in operation or split_marker not in operation or replace_marker not in operation:
        raise ValueError(
            "SEARCH/REPLACE block must contain '<<<<<<< SEARCH', '=======', and '>>>>>>> REPLACE' markers"
        )

    _, _, after_search = operation.partition(search_marker)
    if not after_search:
        raise ValueError("Failed to parse SEARCH section from operation block")

    search_section, split_found, after_split = after_search.partition(split_marker)
    if not split_found:
        raise ValueError("SEARCH/REPLACE block is missing the '=======' separator")

    replace_section, replace_found, _ = after_split.partition(replace_marker)
    if not replace_found:
        raise ValueError("SEARCH/REPLACE block is missing the '>>>>>>> REPLACE' terminator")

    def _normalize(section: str) -> str:
        normalized = section.replace('\r\n', '\n').replace('\r', '\n')
        normalized = textwrap.dedent(normalized)
        if normalized.startswith('\n'):
            normalized = normalized[1:]
        if normalized.endswith('\n'):
            normalized = normalized[:-1]
        return normalized

    search_text = _normalize(search_section)
    replace_text = _normalize(replace_section)

    if not search_text:
        raise ValueError("SEARCH section must contain text to locate")

    haystack = text or ""

    def _locate_span(source: str, needle: str) -> Tuple[int, int]:
        if '...' in needle:
            begin_marker, end_marker = needle.split('...', 1)
            begin_marker = begin_marker.strip()
            end_marker = end_marker.strip()
            if begin_marker and end_marker:
                start = source.find(begin_marker)
                if start == -1:
                    raise ValueError(f"Failed to locate BEGIN marker '{begin_marker}' in target content")
                search_from = start + len(begin_marker)
                end = source.find(end_marker, search_from)
                if end == -1:
                    raise ValueError(
                        f"Failed to locate END marker '{end_marker}' after BEGIN marker '{begin_marker}'"
                    )
                return start, end + len(end_marker)
        start = source.find(needle)
        if start == -1:
            raise ValueError("Failed to locate SEARCH text in target content")
        return start, start + len(needle)

    start_index, end_index = _locate_span(haystack, search_text)
    return haystack[:start_index] + replace_text + haystack[end_index:]
