def normalise_openai_data(data):
    """
    Normalizes the OpenAI response:
    - If data is a list and non-empty, take its first element.
    - Then, if that element is a dict and contains a 'schema' key, return its value.
    - Otherwise, return the data as is.
    """
    if isinstance(data, list):
        if len(data) > 0:
            data = data[0]
        else:
            return None
    if isinstance(data, dict) and "schema" in data:
        return data["schema"]
    return data
