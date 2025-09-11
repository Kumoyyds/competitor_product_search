from .prompt import gen_prompt
from .other_func import check_url

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
          for idx, item in enumerate(results['organic'][:5], 1):  # Top 5 results
              formatted_results.append(
                  f"**{idx}. [{item['title']}]({item['link']})**\n"
                  f"{item.get('snippet', 'No description available')}"
              )
      if not formatted_results:
        return "No results found"
      else:
        formatted_results = [i for i in formatted_results if check_url(i, mkt_plc)]
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




