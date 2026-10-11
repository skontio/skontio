import pytest


@pytest.fixture(autouse=True)
def _isolated_archive(tmp_path, settings):
    """Finalizing writes PDFs/XML to the archive; never let tests touch the real one."""
    settings.ARCHIVE_DIR = tmp_path / "archive"
