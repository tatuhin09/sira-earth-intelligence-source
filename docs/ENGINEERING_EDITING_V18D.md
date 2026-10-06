# SIRA v1.8D — Multi-Language Candidate Editing Foundation

v1.8D adds a new engineering candidate editor without widening SIRA's existing
self-modification or promotion boundary.

Supported candidate source types:

- Python
- JavaScript / TypeScript
- Dart
- Java
- C / C++
- Rust
- Go
- SQL

The editor is project-aware. It enables only languages detected in the source
project, with safe family expansion for JavaScript/TypeScript and C/C++.

Candidate preparation copies only recognized source files and bounded build/test
manifests needed for later verification. It excludes VCS metadata, environment
files, common credential artifacts, symlinks, vendor/dependency trees, build
outputs, runtime/memory data, caches, and hidden directories.

Editing is more restrictive than copying:

- dependency/build manifests are never editable;
- unsupported or foreign-language files are rejected;
- hidden/excluded roots are rejected;
- only root-level source files or explicit application source roots are allowed;
- SIRA protected paths remain protected even through the engineering editor;
- edits are bounded by file count and byte budgets;
- only full UTF-8 text replacements are supported;
- candidate workspace must live outside the main project tree.

The convenience flow prepares the candidate, applies edits, and builds v1.8A
verification plans for every changed file. It does not execute verification,
install packages, modify the main tree, or authorize promotion.

This is a foundation stage. Existing `self_modification.py`, `code_writer.py`,
Evaluator1, Evaluator2, Promotion, autonomous promotion, runtime and CLI are
unchanged. Multi-language model-writer integration comes in the next stage.
