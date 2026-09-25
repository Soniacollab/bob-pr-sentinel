from app.preprocessing import clean_user_input


def test_clean_user_input_standard():
    raw_data = {"username": " Sonia ", "user_score": "85.5"}

    result = clean_user_input(raw_data)

    assert result["username"] == "sonia"
    assert result["user_score"] == 85.5


def test_clean_user_input_missing_score():
    raw_data = {"username": "Alex"}

    result = clean_user_input(raw_data)

    assert result["user_score"] == 0.0