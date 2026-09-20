from process_role import background_enabled, process_role


def test_web_role_disables_background_jobs():
    env = {"BEAUTYBRIDGE_ROLE": "web", "RUN_QUEUE_WORKER": "true"}
    assert process_role(env) == "web"
    assert background_enabled("RUN_QUEUE_WORKER", env) is False


def test_worker_role_enables_background_jobs_by_default():
    env = {"BEAUTYBRIDGE_ROLE": "worker"}
    assert process_role(env) == "worker"
    assert background_enabled("RUN_QUEUE_WORKER", env) is True
    assert background_enabled("RUN_SCHEDULER", env) is True


def test_worker_role_honors_explicit_disable():
    env = {
        "BEAUTYBRIDGE_ROLE": "worker",
        "RUN_QUEUE_WORKER": "false",
        "RUN_SCHEDULER": "0",
    }
    assert background_enabled("RUN_QUEUE_WORKER", env) is False
    assert background_enabled("RUN_SCHEDULER", env) is False


def test_unknown_role_falls_back_to_web():
    env = {"BEAUTYBRIDGE_ROLE": "something-else", "RUN_QUEUE_WORKER": "true"}
    assert process_role(env) == "web"
    assert background_enabled("RUN_QUEUE_WORKER", env) is False
