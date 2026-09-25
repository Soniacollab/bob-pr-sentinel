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


def test_clean_user_input_none_score():
    """Regression: user_score present but None should default to 0.0, not raise TypeError."""
    raw_data = {"username": "Alex", "user_score": None}

    result = clean_user_input(raw_data)

    assert result["user_score"] == 0.0


def test_clean_user_input_empty_string_score():
    """Regression: user_score present but empty string should default to 0.0, not raise ValueError."""
    raw_data = {"username": "Alex", "user_score": ""}

    result = clean_user_input(raw_data)

    assert result["user_score"] == 0.0
