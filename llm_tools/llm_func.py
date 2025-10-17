from .prompt import gen_prompt
from .other_func import check_url, get_pro_name, remove_accents, get_brand, check_found_brand

from dotenv import load_dotenv
import os

from langchain_openai import ChatOpenAI
from langchain_community.tools import GooglePlacesTool
from langchain.agents import initialize_agent, AgentType
from langchain_community.utilities import GoogleSerperAPIWrapper
from langchain_core.tools import tool


# read keys
load_dotenv()
llm_key = os.getenv('QWEN_KEY')
serper_key = os.getenv('SERPER_KEY')

# initialize llm, qwen by default
llm = ChatOpenAI(
    api_key=llm_key,
    temperature=0.1,
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model="qwen-plus-latest",
    extra_body={"enable_search": False}
    )

def do_product_searching(product_name, mkt_plc, country='uk', search_k=5):
  mkt_plc = mkt_plc.lower()
  initial_query = f"{product_name}, site: {mkt_plc}"
  @tool
  def search_refine(query: str) -> str:
      """!don't use it if not necessary ,refined search for inapproriate initial search result"""
      search = GoogleSerperAPIWrapper(serper_api_key=serper_key, gl=country, k=search_k)
      results = search.results(query)
      # Format results as markdown
      formatted_results = []
      if 'organic' in results:
          for idx, item in enumerate(results['organic'][:search_k], 1):
              formatted_results.append(
                  f"**{idx}. [{item['title']}]({item['link']})**\n"
                  f"{item.get('snippet', 'No description available')}"
              )

      brands = get_brand(product_name)
      if not formatted_results:
        return "No results found"
      else:
        # url filtering
        formatted_results = [i for i in formatted_results if check_url(i, mkt_plc)]
        if not formatted_results:
          return "No results found"
        else:
          names = [get_pro_name(i) for i in formatted_results]
          # brand filtering
          formatted_results = [i for i, j in zip(formatted_results, names) if check_found_brand(remove_accents(j[0]), brands) or check_found_brand(remove_accents(j[1]), brands)]
          return "\n\n".join(formatted_results) if formatted_results else "No results found"

  initial_result = search_refine(initial_query)

  if initial_result == 'No results found':
    return 'not found'

  @tool
  def initial_search(query):
    """Run Google Search"""
    result = initial_result
    return result

  # can add refined search if necessary
  tools = [
      initial_search, search_refine
  ]

  agent = initialize_agent(
      tools, llm, agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION, verbose=False
  )


  # generate the prompting
  prompt = gen_prompt(product_name, mkt_plc)

  try_time = 0
  while try_time < 3:
    try:
        agent_response = agent.invoke({"input": prompt})
        result = agent_response['output']

    except Exception:
        result = 'error'

    if result == 'error':
      try_time += 1
      print(f'fail {try_time}')
    else:
      return result

  return 'failed'




### combine it into df

def find_url_llm(df, name_col,  country, web_col='web_1', url_col_name='url_search_1'):
#def find_the_url(df, name_col='item_sku_name_en', web_col='web_1', country='uk', url_col_name='url_search_1'):
  result = []
  df.reset_index(drop=True, inplace=True)
  for i in range(df.shape[0]):
    product_name = df.loc[i, name_col]
    mkt_plc = df.loc[i, web_col]
    r = do_product_searching(product_name, mkt_plc, country=country)
    result.append(r)
  df[url_col_name] = result
  return df
