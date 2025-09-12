import re

def check_url(input_str, pattern):
    def make_regex_literal(s):
        return re.escape(s)

    pattern = make_regex_literal(pattern)
    input_lower = input_str.lower()

    match_inner = re.search(r'\*\*(.*?)\*\*', input_lower)
    if not match_inner:
        return False

    inner_content = match_inner.group(1)

    p_url = r'\(https?://(.*?)\)'
    url_match = re.search(p_url, inner_content)
    if not url_match:
        return False

    url = url_match.group(1)

    if re.search(pattern, url):
        return True

    return False



def get_split_num(num):
  i = 1
  while num // 10 > 0:
    num = num // 10
    i *= 10
  return num * i