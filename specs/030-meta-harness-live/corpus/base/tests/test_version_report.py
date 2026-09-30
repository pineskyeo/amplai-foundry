from demo_app.version_report import summarize


def test_summarize_names_version_and_stage() -> None:
    info = {"package_version": "3.0.0.dev3", "stage": "DEV-03"}
    assert summarize(info) == "amplai 3.0.0.dev3 (DEV-03)"
