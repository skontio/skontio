import pytest


@pytest.mark.django_db
def test_healthz_answers_docker_healthcheck_on_loopback(client, settings):
    # the Dockerfile HEALTHCHECK requests http://127.0.0.1:8000/healthz, which is never in ALLOWED_HOSTS
    settings.ALLOWED_HOSTS = ["skontio.example.org"]
    response = client.get("/healthz", HTTP_HOST="127.0.0.1:8000")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_host_check_still_applies_to_other_paths(client, settings):
    settings.ALLOWED_HOSTS = ["skontio.example.org"]
    response = client.get("/", HTTP_HOST="127.0.0.1:8000")
    assert response.status_code == 400
