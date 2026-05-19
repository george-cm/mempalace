"""
Tests for mempalace.diary_ingest.

WARN-010: state-file load exception must be logged via logger.warning.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# WARN-010: silent except in state-file load → logger.warning
# ---------------------------------------------------------------------------


def test_ingest_diaries_logs_warning_on_bad_state_file(tmp_path):
    """WARN-010: ingest_diaries must call logger.warning when the state file
    contains invalid JSON.

    Before the fix the except block is silent (``state = {}`` only).
    After the fix it calls ``logger.warning(...)`` before falling back.
    """
    from mempalace.diary_ingest import ingest_diaries

    # Minimal diary directory with one parseable file.
    diary_dir = tmp_path / "diary"
    diary_dir.mkdir()
    (diary_dir / "2024-01-01.md").write_text(
        "## Morning Entry\n" + "word " * 25 + "\n## Afternoon\n" + "word " * 25
    )

    palace_path = tmp_path / "palace"

    # A corrupt state file that will trigger the except block.
    corrupt_state_file = tmp_path / "diary_state.json"
    corrupt_state_file.write_text("{not valid json!!!")

    # Stub out ChromaDB / locking so we don't need a real palace on disk.
    fake_col = MagicMock()
    fake_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}

    with patch(
        "mempalace.diary_ingest._state_file_for", return_value=corrupt_state_file
    ):
        with patch("mempalace.diary_ingest.get_collection", return_value=fake_col):
            with patch(
                "mempalace.diary_ingest.get_closets_collection", return_value=fake_col
            ):
                with patch("mempalace.diary_ingest.mine_lock") as mock_lock:
                    mock_lock.return_value.__enter__ = MagicMock(return_value=None)
                    mock_lock.return_value.__exit__ = MagicMock(return_value=False)
                    with patch("mempalace.diary_ingest.purge_file_closets"):
                        with patch("mempalace.diary_ingest.upsert_closet_lines"):
                            with patch(
                                "mempalace.diary_ingest.logger", create=True
                            ) as mock_logger:
                                ingest_diaries(str(diary_dir), str(palace_path))

    mock_logger.warning.assert_called_once()
