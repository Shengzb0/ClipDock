import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    from clipdock import app as app_module

    app = QApplication.instance() or QApplication([])
    app_module._set_app_font(app)
    return app
