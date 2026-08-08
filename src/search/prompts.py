def gen_prompt(product_name, mkt_plc):
  result = f"""
  you are an online retailer now
  now you are doing market research, find the url of the product on the given marketplace.

  the product name is: {product_name}
  the marketplace is: {mkt_plc}

  expected output:
  only the url if you found it,
  'not found' if not found

  workflow:
  use google search to search {product_name} on {mkt_plc},
  input ex: {product_name} on {mkt_plc}

  select the url of the product page on {mkt_plc}

  note:
  1.strict match, be careful about the details like quantity, size, color, model, brand etc.

  """
  return result