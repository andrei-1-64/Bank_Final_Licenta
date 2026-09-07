from app.core.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)


def test_hash_password_roundtrip():
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert verify_password("correct horse battery staple", hashed) is True
    assert verify_password("wrong password", hashed) is False


def test_hash_password_unique_salts():
    assert hash_password("same-password") != hash_password("same-password")


def test_verify_password_rejects_malformed_hash():
    assert verify_password("anything", "not-a-real-bcrypt-hash") is False


def test_generate_session_token_is_unique_and_url_safe():
    tokens = {generate_session_token() for _ in range(50)}
    assert len(tokens) == 50
    for token in tokens:
        assert len(token) > 20


def test_hash_session_token_deterministic_and_sensitive():
    token = generate_session_token()
    assert hash_session_token(token) == hash_session_token(token)
    assert hash_session_token(token) != hash_session_token(generate_session_token())
