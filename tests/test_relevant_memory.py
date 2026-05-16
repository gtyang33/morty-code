from __future__ import annotations

from morty_code.memory.relevant_memory import RelevantMemoryFinder


def test_relevant_memory_finder_ranks_stronger_matches_before_name_order(tmp_path) -> None:
    (tmp_path / "aaa-weak.md").write_text(
        "This memory mentions calcite once.",
        encoding="utf-8",
    )
    (tmp_path / "zzz-strong.md").write_text(
        "Calcite materialized view recommendation uses aggregate rewrite rules.",
        encoding="utf-8",
    )

    matches = RelevantMemoryFinder(tmp_path, max_files=1).find(
        "calcite materialized view aggregate"
    )

    assert len(matches) == 1
    assert matches[0].payload["path"].endswith("zzz-strong.md")
