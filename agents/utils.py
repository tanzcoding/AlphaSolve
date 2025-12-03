

def build_conjuecture_helper(resp_from_llm, begin_str, end_str):
    bindex = resp_from_llm.find(begin_str) + len(begin_str)
    eindex = resp_from_llm.find(end_str)

    if bindex < len(begin_str) or eindex < 0:
        print('illegal respronse for problem missing ', begin_str, ' or ', end_str, ' begin index ', bindex, ' end index ', eindex)
        return None

    return resp_from_llm[bindex: eindex]



def load_prompt_from_file(prompt_file_path):

    f = open(prompt_file_path, 'r')
    prompt_template = f.read()

    return prompt_template
