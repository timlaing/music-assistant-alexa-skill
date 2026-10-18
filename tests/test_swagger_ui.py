"""Tests for the Swagger UI / OpenAPI spec endpoints."""

from swagger_ui import OPENAPI_SPEC

AUTH = {"Authorization": "Basic dGVzdC11c2VyOnRlc3QtcGFzc3dvcmQ="}


def test_openapi_spec_is_valid_json_with_documented_paths(client):
    response = client.get("/openapi.json", headers=AUTH)
    assert response.status_code == 200
    assert response.is_json
    spec = response.json
    assert spec["openapi"] == "3.0.0"
    assert spec["info"]["title"] == "Music Assistant API"
    assert set(spec["paths"]) == {"/ma/push-url", "/ma/latest-url", "/"}


def test_openapi_spec_documents_required_stream_url(client):
    schema = client.get("/openapi.json", headers=AUTH).json["paths"]["/ma/push-url"][
        "post"
    ]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["required"] == ["streamUrl"]
    assert "streamUrl" in schema["properties"]


def test_openapi_module_constant_matches_endpoint(client):
    assert client.get("/openapi.json", headers=AUTH).json == OPENAPI_SPEC


def test_docs_renders_html_pointing_at_openapi_json(client):
    response = client.get("/docs", headers=AUTH)
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    body = response.get_data(as_text=True)
    assert "SwaggerUIBundle" in body
    assert "swagger-ui" in body
    # The rendered template injects the absolute spec URL for the current host.
    assert "http://localhost/openapi.json" in body
