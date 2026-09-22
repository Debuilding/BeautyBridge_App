from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_web_service_has_web_role_and_never_starts_worker():
    text = (ROOT / "deploy/systemd/beautybridge.service").read_text()
    assert 'Environment="BEAUTYBRIDGE_ROLE=web"' in text
    assert 'Environment="RUN_QUEUE_WORKER=false"' in text
    assert "gunicorn" in text
    assert "worker.py" not in text


def test_worker_service_has_worker_role_and_runs_worker_entrypoint():
    text = (ROOT / "deploy/systemd/beautybridge-worker.service").read_text()
    assert 'Environment="BEAUTYBRIDGE_ROLE=worker"' in text
    assert 'Environment="RUN_QUEUE_WORKER=true"' in text
    assert "python worker.py" in text


def test_deployment_manifests_do_not_contain_known_secret_placeholders():
    for path in (
        ROOT / "deploy/systemd/beautybridge.service",
        ROOT / "deploy/systemd/beautybridge-worker.service",
    ):
        text = path.read_text().lower()
        assert "api_key=" not in text
        assert "password=" not in text
        assert "access_token=" not in text
