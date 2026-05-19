"""Tests for mempalace.palace - palace operations."""

from unittest.mock import MagicMock


# --- WARN-001: file_already_mined IndexError on mismatched metadatas ---


class TestFileAlreadyMined:
    def _make_collection(self, ids, metadatas):
        col = MagicMock()
        col.get.return_value = {"ids": ids, "metadatas": metadatas}
        return col

    def test_returns_true_when_file_is_current(self, tmp_path):
        from mempalace.palace import file_already_mined

        src = tmp_path / "file.txt"
        src.write_text("hello")
        import os, time

        mtime = os.path.getmtime(str(src))
        col = self._make_collection(
            ids=["abc"],
            metadatas=[{"normalize_version": 999, "source_mtime": str(mtime)}],
        )
        assert file_already_mined(col, str(src), check_mtime=True) is True

    def test_returns_false_when_no_ids(self, tmp_path):
        from mempalace.palace import file_already_mined

        col = MagicMock()
        col.get.return_value = {"ids": [], "metadatas": []}
        assert file_already_mined(col, str(tmp_path / "x.txt")) is False

    def test_does_not_raise_when_metadatas_empty_but_ids_present(self, tmp_path):
        """WARN-001: ids non-empty but metadatas empty must not raise IndexError."""
        from mempalace.palace import file_already_mined

        # ChromaDB returns non-empty ids but empty metadatas (transient inconsistency)
        col = self._make_collection(ids=["some-id"], metadatas=[])
        result = file_already_mined(col, str(tmp_path / "x.txt"))
        # Must return a bool, not raise IndexError
        assert isinstance(result, bool)

    def test_does_not_raise_when_metadatas_is_none(self, tmp_path):
        """WARN-001: None metadatas must not raise."""
        from mempalace.palace import file_already_mined

        col = MagicMock()
        col.get.return_value = {"ids": ["some-id"], "metadatas": None}
        result = file_already_mined(col, str(tmp_path / "x.txt"))
        assert isinstance(result, bool)
