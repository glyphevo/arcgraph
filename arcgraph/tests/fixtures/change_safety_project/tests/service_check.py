from change_safety_app.service import change_status


def verify_change_status() -> None:
    assert change_status("change-1") == {
        "change_id": "change-1",
        "status": "planned",
    }
