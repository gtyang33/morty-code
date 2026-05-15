# Memory Management Design

## Goal

Improve Morty Code memory management so automatic memory writes are useful, layered, and low-noise. The current implementation turns assistant text into truncated summaries and writes the same content to both session and durable memory. The new behavior should classify candidate memories and write them only to the appropriate layer.

## Scope

This design covers local rule-based extraction and write routing. It does not add model-based summarization, embeddings, external services, or a new memory file format.

## Architecture

`MemoryExtractor` becomes a candidate classifier instead of a string truncator. It returns structured candidates with:

- `text`: normalized memory text.
- `target`: either `session` or `durable`.
- `topic`: a broad category such as `preference`, `constraint`, `environment`, `decision`, or `task`.
- `confidence`: a heuristic score for debugging and future tuning.
- `reason`: a short explanation of why the candidate was kept.

`QueryEngine._write_memories()` routes each candidate by target:

- `session` candidates are appended to `SessionMemoryStore`.
- `durable` candidates are appended to `DurableMemoryStore`.
- the same candidate is never written to both stores.

The existing durable memory layout remains unchanged: topic files hold full entries and `MEMORY.md` remains a bounded index.

## Classification Rules

Durable memory is reserved for information likely to matter across sessions:

- explicit user preferences, such as preferred language, workflow, or style.
- stable project constraints and conventions.
- stable environment facts.
- explicit "remember this" style instructions.

Session memory is for current-task context:

- facts discovered while reading the repository.
- implementation decisions made during the current task.
- current working state that may help after compaction or in later turns of the same session.

The extractor skips:

- `Echo:` output.
- runtime or API errors.
- generic assistant explanations.
- empty, very short, or excessively long content.
- tool chatter and text that looks like stack traces or command output.
- duplicate normalized candidates within a single extraction pass.

## Data Flow

1. A query iteration produces new messages.
2. `QueryEngine._write_memories()` passes those messages to `MemoryExtractor.extract()`.
3. The extractor inspects assistant text blocks and creates zero or more `MemoryCandidate` values.
4. The query engine appends each candidate only to its target store.
5. Prompt building and relevant-memory retrieval continue to read the existing session and durable memory files.

## Error Handling

Memory extraction and writing remain best-effort. Empty or skipped candidates do not raise. Existing store behavior is preserved, and failures should not interrupt the main conversation loop.

## Testing

Focused unit tests should cover:

- ordinary assistant replies are skipped.
- explicit long-term preferences are classified as durable.
- current-task discoveries are classified as session.
- runtime errors and echo responses are skipped.
- duplicate candidates are collapsed.
- query-engine memory writes route candidates to exactly one store.

## Non-Goals

- no semantic search or embedding index.
- no LLM call to summarize memory.
- no migration of existing memory files.
- no redesign of relevant memory attachments in this change.
