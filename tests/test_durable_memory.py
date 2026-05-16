from __future__ import annotations

from pathlib import Path

from morty_code.memory.durable_memory import DurableMemoryStore


def test_append_summary_writes_frontmatter_topic_file_and_index(tmp_path: Path) -> None:
    store = DurableMemoryStore(tmp_path)

    store.append_summary(
        "Remember: the user prefers concise Chinese responses.",
        memory_type="user",
    )

    topic_files = [path for path in tmp_path.glob("*.md") if path.name != "MEMORY.md"]
    assert len(topic_files) == 1
    topic_text = topic_files[0].read_text(encoding="utf-8")
    assert topic_text.startswith("---\n")
    assert 'name: "Remember the user prefers concise Chinese responses"\n' in topic_text
    assert 'description: "Remember: the user prefers concise Chinese responses."\n' in topic_text
    assert "type: user\n" in topic_text
    assert "- Remember: the user prefers concise Chinese responses.\n" in topic_text

    index_text = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert f"]({topic_files[0].name})" in index_text
    assert "type: user" not in index_text


def test_append_summary_deduplicates_existing_memory_entry(tmp_path: Path) -> None:
    store = DurableMemoryStore(tmp_path)
    summary = "Project constraint: use real database tests for migrations."

    store.append_summary(summary, memory_type="feedback")
    store.append_summary(summary, memory_type="feedback")

    topic_file = next(path for path in tmp_path.glob("*.md") if path.name != "MEMORY.md")
    topic_text = topic_file.read_text(encoding="utf-8")
    assert topic_text.count("---\n") == 2
    assert topic_text.count(f"- {summary}\n") == 1
    index_text = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert index_text.count(f"]({topic_file.name})") == 1


def test_durable_memory_max_index_lines_one_keeps_only_header(tmp_path: Path) -> None:
    store = DurableMemoryStore(tmp_path, max_index_lines=1, max_index_bytes=24000)

    store.append_summary("Remember: first durable memory.", memory_type="user")
    store.append_summary("Remember: second durable memory.", memory_type="user")

    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "# Memory Index\n"


def test_durable_memory_quotes_frontmatter_scalars(tmp_path: Path) -> None:
    store = DurableMemoryStore(tmp_path)

    store.append_summary("Remember: use key: value syntax in examples.", memory_type="project")

    topic_file = next(path for path in tmp_path.glob("*.md") if path.name != "MEMORY.md")
    topic_text = topic_file.read_text(encoding="utf-8")
    assert 'name: "Remember use key value syntax in examples"\n' in topic_text
    assert 'description: "Remember: use key: value syntax in examples."\n' in topic_text
