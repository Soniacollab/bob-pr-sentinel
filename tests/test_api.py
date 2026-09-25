from app.api import process_registration


def test_process_registration_valid():
    payload = {"username": "TestUser", "user_score": "100"}

    response = process_registration(payload)

    assert response["status"] == "success"
    assert response["processed_score"] == 100.0