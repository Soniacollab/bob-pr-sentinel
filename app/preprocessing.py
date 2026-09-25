def clean_user_input(data: dict) -> dict:
    """
    Nettoie et formate les données reçues par l'API.
    """
    cleaned_data = {}

    if "user_score" in data:
        value = data["user_score"]

        if value is None or value == "":
            cleaned_data["user_score"] = 0.0
        else:
            cleaned_data["user_score"] = float(value)
    else:
        cleaned_data["user_score"] = 0.0

    cleaned_data["username"] = str(
        data.get("username", "")
    ).strip().lower()

    return cleaned_data