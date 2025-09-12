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


use_match = config.get('use_match', None)
if use_match not in [0, 1]:
    raise ValueError("Please set the use_match parameter to 0 or 1 in the config.yaml file.")

if use_match == 0:
    print("\n\n no semantic matching mode \n\n")
else:
    print("\n\n semantic matching mode \n\n")

match_file_path = config.get('match_file', None)
if use_match==1 and not match_file_path:
    raise ValueError("Please provide the match_file in the config.yaml file if using match.")

if use_match:

    match_file_path = os.path.join(os.getcwd(), f"input/{match_file_path}")
    match_file = pd.read_excel(match_file_path)
    # make sure the category and web col name unified as well 
    web_col = config.get('match_web_col', None)
    cate_col = config.get('match_cate_col', None)
    if not web_col or not cate_col:
        raise ValueError("Please provide the match_web_col and match_cate_col in the config.yaml file if using match.")
    match_file.rename(columns={web_col: 'web', cate_col: 'Category'}, inplace=True)
    match_file['Category'] = match_file['Category'].apply(lambda x: x.strip())
    # the cate used to do match should be defined as well 

    match_dict = {}
    for i in range(match_file.shape[0]):
        match_dict[match_file.iloc[i, 0]] = list(match_file.iloc[i, 1:])

else:
    # be in the yaml
    web = config.get('web', None)
    if not web:
        raise ValueError("Please provide the web name in the config.yaml file if not using match.")

    file['web_1'] = web


if use_match:
    print('\nloading the semantic matching model\n')
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer('sentence-transformers/all-mpnet-base-v2')
    def embedding_one(text):
        return model.encode(text)
    print("\nloading done\n")
# the categories associated with files
    file_cate = config.get('input_cate_col', None)
    if not file_cate:
        raise ValueError("Please provide the input_cate_col in the config.yaml file if using match.")
    file.rename(columns={file_cate: 'suggest_cate_1'}, inplace=True)
    file_embedding = {}
    print("\nstart embedding for the file category\n")
    for i in tqdm(set(file['suggest_cate_1']), total=len(set(file['suggest_cate_1']))):
        file_embedding[i] = embedding_one(i)
    print("\n embedding done\n")

# the categories associated with the web
    cate_embedding = {}
    print("\nstart embedding for the match_file category\n")
    for i in tqdm(match_dict, total=len(match_dict)):
        cate_embedding[i] = embedding_one(i)
    print("\n embedding done\n")
    cate_list = [i for i in match_dict]
    cate_matrix = np.vstack([cate_embedding[i] for i in cate_list])

    file.reset_index(drop=True, inplace=True)

    cate_col = 'suggest_cate_1'
    n_web = len(match_dict[list(match_dict.keys())[0]])

    results = [[] for i in range(n_web)]
    match_c = []
    sim_v = []
    for i in range(file.shape[0]):
        v = file_embedding[file.loc[i, cate_col]]
    # the similarity
    sim = cate_matrix @ v

    # max similarity
    sim_max_i = np.argmax(sim)

    # matched_cate
    c = cate_list[sim_max_i]

    sim_v.append(sim[sim_max_i])

    match_c.append(c)
    item = match_dict[c]
    for j in range(n_web):
        results[j].append(item[j])

    file['match_c'] = match_c
    file['sim'] = sim_v
    for i in range(n_web):
        file[f"web_{i+1}"] = results[i]   


    ### setting in yaml
    sim_cri = config.get('sim_cri', 0.5)
    mask = (file['sim']>sim_cri) 
    # done
    file = file.loc[mask]
    file.reset_index(drop=True, inplace=True)
    

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


round = split_num//1000 + 1
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

