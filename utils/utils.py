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
    Apply a unified diff to the original text.

    Args:
        original_text: The original text to apply the diff to
        diff_text: The unified diff format text

    Returns:
        The modified text after applying the diff

    Raises:
        ValueError: If the diff cannot be applied or is in invalid format
    """
    import re

    if not original_text:
        return original_text

    if not diff_text:
        return original_text
    
    # Check for common mistakes: diff wrapped in XML tags
    if '<proof>' in diff_text or '<conjecture>' in diff_text:
        raise ValueError(
            "Invalid diff format: detected XML tags like <proof> or <conjecture>. "
            "You must use unified diff format with lines starting with '+' (add) or '-' (delete), "
            "NOT full text replacement wrapped in XML tags."
        )

    lines = original_text.split('\n')
    diff_lines = diff_text.split('\n')

    i = 0
    new_lines = []
    hunks = []

    # Parse unified diff to find hunks
    current_hunk = None
    hunk_start = 0
    hunk_old_start = 0
    hunk_old_count = 0
    hunk_new_count = 0

    in_hunk = False
    has_diff_markers = False  # Track if we found any +/- lines

    for line in diff_lines:
        # Unified diff header: @@ -old_start,old_count +new_start,new_count @@
        hunk_match = re.match(r'^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@', line)
        if hunk_match:
            # Save previous hunk if exists
            if current_hunk is not None:
                hunks.append(current_hunk)

            hunk_old_start = int(hunk_match.group(1))
            hunk_old_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
            hunk_new_start = int(hunk_match.group(3))
            hunk_new_count = int(hunk_match.group(4)) if hunk_match.group(4) else 1

            current_hunk = {
                'old_start': hunk_old_start,
                'old_count': hunk_old_count,
                'new_start': hunk_new_start,
                'new_count': hunk_new_count,
                'lines': []
            }
            in_hunk = True
            continue

        if in_hunk and current_hunk is not None:
            if line.startswith('+') and not line.startswith('+++'):
                current_hunk['lines'].append(('add', line[1:]))
                has_diff_markers = True
            elif line.startswith('-') and not line.startswith('---'):
                current_hunk['lines'].append(('del', line[1:]))
                has_diff_markers = True
            elif line.startswith(' '):
                current_hunk['lines'].append(('ctx', line[1:]))
            elif line.startswith('@@'):
                # End of current hunk, start new one (shouldn't happen in valid diff)
                hunks.append(current_hunk)
                hunk_match = re.match(r'^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@', line)
                if hunk_match:
                    hunk_old_start = int(hunk_match.group(1))
                    hunk_old_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
                    hunk_new_start = int(hunk_match.group(3))
                    hunk_new_count = int(hunk_match.group(4)) if hunk_match.group(4) else 1
                    current_hunk = {
                        'old_start': hunk_old_start,
                        'old_count': hunk_old_count,
                        'new_start': hunk_new_start,
                        'new_count': hunk_new_count,
                        'lines': []
                    }
            # Skip lines starting with --- or +++ that are part of diff header
            elif line.startswith('---') or line.startswith('+++'):
                continue

    # Don't forget to add the last hunk
    if current_hunk is not None:
        hunks.append(current_hunk)
    
    # Validate that we found actual diff content
    if not has_diff_markers:
        raise ValueError(
            "Invalid diff format: no lines starting with '+' or '-' found. "
            "Unified diff must contain lines starting with '+' (additions) or '-' (deletions). "
            "Make sure you're using proper diff syntax, not plain text."
        )

    # Apply hunks to the original text
    for hunk in hunks:
        old_start = hunk['old_start'] - 1  # Convert to 0-indexed
        old_count = hunk['old_count']

        new_lines = []

        # Add lines before this hunk
        while i < old_start and i < len(lines):
            new_lines.append(lines[i])
            i += 1

        # Process the hunk
        for line_type, content in hunk['lines']:
            if line_type == 'add':
                new_lines.append(content)
            elif line_type == 'del':
                # Skip the original line
                i += 1
            elif line_type == 'ctx':
                new_lines.append(content)
                i += 1

        # Add remaining lines from old text that are part of deleted section
        # but not explicitly in the hunk
        while i < old_start + old_count and i < len(lines):
            new_lines.append(lines[i])
            i += 1

    # Add any remaining lines after the last hunk
    while i < len(lines):
        new_lines.append(lines[i])
        i += 1

    return '\n'.join(new_lines)


def load_prompt_from_file(prompt_file_path):
    with open(prompt_file_path, 'r', encoding='utf-8') as f:
        return f.read()
