def extract_substring(target_string, begin_str, end_str, *, logger=None, module="agents.utils"):
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

def load_prompt_from_file(prompt_file_path):

    f = open(prompt_file_path, 'r', encoding='utf-8')
    prompt_template = f.read()

    return prompt_template
