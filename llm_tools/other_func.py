import re
import unicodedata
import pandas as pd
import os

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

# to remove accent 
def remove_accents(input_str):
    nfkd_form = unicodedata.normalize('NFD', input_str)
    # Filter out combining characters (diacritics)
    return ''.join([c for c in nfkd_form if not unicodedata.combining(c)])

# extract the product name and descrition from google search results for later filtering based on brand name
def get_pro_name(string):
  pattern = r'\[(.*?)\]'
  match_inner = re.search(pattern, string)
  product_name = match_inner.group(1)

  try:
    description = string.split(")**\n")[-1]
  except:
    description = " "
  return product_name, description


# never change the banrd name

brand_path = os.path.join(os.getcwd(), f"llm_tools/brand.xlsx")
print(brand_path)
brands = pd.read_excel(brand_path)
brands_set = set(brands['brandname_en']) | set(brands['brandname_cn']) | set(brands['brandname_full'])

# only check

def get_brand(name1, brand_set=brands_set):
  result = []
  for brand in brand_set:
    pattern = rf"\b{brand}\b"
    if re.search(pattern.lower(), name1.lower()):
      result.append(brand)
  return result

# only found
def check_found_brand(name2, checked_brands):
  if not checked_brands:
    return True
  else:
    for brand in checked_brands:
      pattern = rf"\b{brand}\b"
      if re.search(pattern.lower(), name2.lower()):
        return True
    return False