# competitor_product_search
This is to help online retailer find the url of product on competitor  platforms

# core logic 

- **reaching options** using search engine, ex: goolge search, bing search.  
    - currently using [*serper*](https://serper.dev/login) for google search (cheapest, but stable provider). other options: *exa*, *tavily*, *barve*, *bing serach api(retired however)*, *duckduckgo(free, but not stable)*

- **option selection** using llm to pick the matching option provided by the search engine.

# How to use 
1. go to the dir.  
`cp competitor_product_search`   

2. prepare the env.  
`pip install -r requirements.txt`  
  
3. prepare the .env file  
`cp env.sample .env`  

    and then fill your api keys.  
    get your api keys: [qwen](https://bailian.console.aliyun.com/?utm_content=se_1021228063&gclid=EAIaIQobChMIy6uAor6qkAMVlYi5BR3ehBiREAAYASAAEgL5YfD_BwE#/home), [serper](https://serper.dev/login)
  
4. make sure your file ready.  
    things to check:
    - **sku name**, you should test which language works well, ex: for amazon.fr, most product names are in French, and after testing on google, french product name is indeed easier to be reached. then we should use french sku name. 
    - **target web**, ex: amazon.fr, tesco.com, etc. make sure you give the right one, and make sure the suffix is correct.
    - **be in .xlsx**

5. put your file in the **input/** directory  
6. set the **config.yaml** following the instruction inside. 
7. run the **main.py**  
`python main.py`
8. get your result in the dir **output/** (result.xlsx)   
ps: the **output_partitions** there is to store the result partitions and then combine them to result.   

# note  
- **brand.xlsx** under **llm_tools/** needs maintenance, like when there are new brands, add them to the file.  

- always try some sample, like 50/ 100 before running the whole file, in case anything going unexpectedly like the search engine doesn't work well for the target web or some parameters are set wrongly, in case of wasting searchs. **remember, every search costs**.  
(ex: serper, even the most cost-effective api provider i found, 50000credit/50$. the llm has token cost as well)


## furture work to be done
1. **integrated search api**, you can have different results using different search api provider (based on the search engine). ex:  chinese ambient food works well on duckduckgo but not well on google.  

2. **filtering algo**, tbh i have done almost the most for text-only dimension, vision stuff could be added in the future, with vision stuff, even efforts on reviewing process could be saved as well.  
  
3. **local host llm**, this is mostly for the long-term internal security consideration. deployment is not complicated, go to [vllm](https://docs.vllm.ai/en/latest/)




Is there still any issue, contact **Yuding** by wechat (mylordship), all the best.  
