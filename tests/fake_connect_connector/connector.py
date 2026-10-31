def connect(answers: dict) -> dict:
    api_key = answers["api_key"].strip()
    if not api_key:
        raise ValueError("Enter your API key.")
    if answers["region"] != "eu":
        raise ValueError("Region must be eu.")
    return {"secrets": {"api_key": api_key}}
