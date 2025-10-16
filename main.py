import os
import pandas as pd
import pandas as pd
import numpy as np
from tqdm import tqdm
import concurrent.futures
from llm_tools.llm_func import find_url_llm
import yaml
from llm_tools.other_func import get_split_num

print("all modules imported, let's go!")

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)


# input path
input_file_name = config['input_file']
input_path = os.path.join(os.getcwd(), f"input/{input_file_name}")
file = pd.read_excel(input_path)

# output_partition path
output_p_root = os.path.join(os.getcwd(), "output/output_partitions/")




# be in the yaml
web = config.get('web', None)
if not web:
    raise ValueError("Please provide the web name in the config.yaml file if not using match.")

file['web_1'] = web



name_col = config.get('input_sku_name_col', None)
country = config.get('country', None)

if not name_col or not country:
    raise ValueError("Please provide the input_sku_name_col and country in the config.yaml file.")


##### yaml definition
def find_the_url(df):
  df = find_url_llm(df, name_col=name_col, web_col='web_1', country=country, url_col_name='url_search_1')
  return df


split_num = get_split_num(file.shape[0])
files = np.array_split(file, split_num)


round = split_num//500 + 1
chunk = int(split_num/round)
for i in range(round):
  print(f"round {i+1} begins, left:{round-i-1}")
  dfs = files[i*chunk:(i+1)*chunk]
  print("\ndo the searching\n")
  with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
    dfs = list(tqdm(executor.map(find_the_url, dfs), total=chunk))
    print("\nsearching done\n")

  df = pd.concat(dfs, axis=0)
  df.reset_index(drop=True, inplace=True)
  # after 30000, use 2
  df.to_excel(output_p_root+f'/result_{i}.xlsx', index=False)



output_file_name = config.get('output_file', None)
if not output_file_name:
    raise ValueError("Please provide the output_file in the config.yaml file.")

output_result_path = os.path.join(os.getcwd(), f"output/{output_file_name}")


dfs = []
print("\nstart combining the partition results\n")
for i in tqdm(range(round)):
  df = pd.read_excel(output_p_root+f'result_{i}.xlsx')
  dfs.append(df)
dfs = pd.concat(dfs, axis=0)
print("\ncombining done\n")
dfs.reset_index(drop=True, inplace=True)

print("\nstart saving the final result\n")
dfs.to_excel(output_result_path, index=False)
print("\nsaving done\n")
print(f"please find your result in {output_result_path}")

