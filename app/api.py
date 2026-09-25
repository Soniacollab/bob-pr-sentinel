from app.preprocessing import clean_user_input


def process_registration(payload: dict) -> dict:
    cleaned = clean_user_input(payload)

    return {
        "status": "success",
        "processed_score": cleaned["user_score"],
        "user": cleaned["username"],
    }