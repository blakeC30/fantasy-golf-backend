"""
Tests for authentication endpoints.

Covers: register, login, token refresh, /users/me, and error cases.
"""

import pytest


class TestRegister:
    def test_register_success(self, client):
        resp = client.post("/api/v1/auth/register", json={
            "email": "new@example.com",
            "password": "strongpass",
            "display_name": "New User",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_register_duplicate_email(self, client):
        payload = {"email": "dup@example.com", "password": "pass", "display_name": "Dup"}
        client.post("/api/v1/auth/register", json=payload)
        resp = client.post("/api/v1/auth/register", json=payload)
        assert resp.status_code == 409

    def test_register_normalises_email_to_lowercase(self, client):
        """Email should be stored in lowercase regardless of input."""
        resp = client.post("/api/v1/auth/register", json={
            "email": "Upper@Example.COM",
            "password": "pass",
            "display_name": "Upper",
        })
        assert resp.status_code == 201
        # Should be able to log in with lowercase version.
        login = client.post("/api/v1/auth/login", json={
            "email": "upper@example.com",
            "password": "pass",
        })
        assert login.status_code == 200


class TestLogin:
    def test_login_success(self, client):
        client.post("/api/v1/auth/register", json={
            "email": "login@example.com",
            "password": "correctpass",
            "display_name": "Login",
        })
        resp = client.post("/api/v1/auth/login", json={
            "email": "login@example.com",
            "password": "correctpass",
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_login_wrong_password(self, client):
        client.post("/api/v1/auth/register", json={
            "email": "wrong@example.com",
            "password": "correctpass",
            "display_name": "Wrong",
        })
        resp = client.post("/api/v1/auth/login", json={
            "email": "wrong@example.com",
            "password": "wrongpass",
        })
        assert resp.status_code == 401

    def test_login_unknown_email(self, client):
        resp = client.post("/api/v1/auth/login", json={
            "email": "nobody@example.com",
            "password": "anything",
        })
        assert resp.status_code == 401

    def test_login_sets_refresh_cookie(self, client):
        client.post("/api/v1/auth/register", json={
            "email": "cookie@example.com",
            "password": "pass",
            "display_name": "Cookie",
        })
        resp = client.post("/api/v1/auth/login", json={
            "email": "cookie@example.com",
            "password": "pass",
        })
        assert "refresh_token" in resp.cookies


class TestMe:
    def test_get_me_authenticated(self, client, auth_headers):
        resp = client.get("/api/v1/users/me", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "test@example.com"
        assert data["display_name"] == "Test User"
        assert "password_hash" not in data
        assert "google_id" not in data

    def test_get_me_no_token(self, client):
        resp = client.get("/api/v1/users/me")
        assert resp.status_code == 401

    def test_get_me_invalid_token(self, client):
        resp = client.get("/api/v1/users/me", headers={"Authorization": "Bearer notavalidtoken"})
        assert resp.status_code == 401


class TestRefresh:
    def test_refresh_returns_new_access_token(self, client):
        client.post("/api/v1/auth/register", json={
            "email": "refresh@example.com",
            "password": "pass",
            "display_name": "Refresh",
        })
        login = client.post("/api/v1/auth/login", json={
            "email": "refresh@example.com",
            "password": "pass",
        })
        # TestClient stores cookies automatically.
        refresh = client.post("/api/v1/auth/refresh")
        assert refresh.status_code == 200
        assert "access_token" in refresh.json()

    def test_refresh_without_cookie_returns_401(self, client):
        resp = client.post("/api/v1/auth/refresh")
        assert resp.status_code == 401
