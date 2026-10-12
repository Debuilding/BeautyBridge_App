from flow_guard import next_flow_reply


def test_waiting_admin_confirmation_reply_includes_request_details():
    reply = next_flow_reply(
        {
            "services": {"manicure": {"name": "Манікюр"}},
        },
        {
            "state": "WAITING_ADMIN_CONFIRMATION",
            "service_id": "manicure",
            "date": "2026-10-14",
            "time": "11:00",
        },
    )

    assert "Манікюр" in reply
    assert "2026-10-14" in reply
    assert "11:00" in reply
    assert "уточню доступність" in reply.lower()
    assert "адміністратор" not in reply
    assert "передала заявку" not in reply


def test_waiting_client_time_confirmation_asks_for_yes_or_no():
    reply = next_flow_reply({}, {"state": "WAITING_CLIENT_TIME_CONFIRMATION"})
    assert "підходить" in reply.lower()
    assert "так" in reply.lower()
    assert "ні" in reply.lower()
