from __future__ import annotations

import ast
from difflib import SequenceMatcher
from pathlib import Path
import re
import subprocess
import tempfile
from difflib import unified_diff
from typing import Iterable, Mapping, Sequence

from .task import context_path_and_range, normalize_relative_path


class PatchError(ValueError):
    """Raised when a worker patch cannot be safely applied."""


class ScopeViolationError(PatchError):
    def __init__(self, paths: Iterable[str]) -> None:
        self.paths = tuple(sorted(set(paths)))
        super().__init__(
            "patch changes paths outside allowed_files: " + ", ".join(self.paths)
        )


_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
_VALID_INDEX_LINE = re.compile(
    r"^index [0-9a-fA-F]+\.\.[0-9a-fA-F]+(?: [0-7]{6})?$"
)
_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?P<suffix>.*)$"
)
_GIT_TIMEOUT_SECONDS = 30.0


def _marker_path(value: str) -> str | None:
    token = value.strip().split("\t", 1)[0].strip()
    if token == "/dev/null":
        return None
    if token.startswith("a/") or token.startswith("b/"):
        token = token[2:]
    try:
        return normalize_relative_path(token)
    except ValueError as exc:
        raise PatchError(f"invalid patch path: {token!r}") from exc


def patch_paths(patch: str) -> tuple[str, ...]:
    if not isinstance(patch, str) or not patch.strip():
        raise PatchError("worker returned an empty patch")
    found: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            match = _DIFF_HEADER.match(line)
            if not match:
                raise PatchError("malformed diff --git header")
            for value in match.groups():
                path = _marker_path(value)
                if path:
                    found.append(path)
        elif line.startswith("--- ") or line.startswith("+++ "):
            path = _marker_path(line[4:])
            if path:
                found.append(path)
        elif line.startswith("rename from ") or line.startswith("rename to "):
            path = _marker_path(line.split(" ", 2)[2])
            if path:
                found.append(path)
    unique = tuple(dict.fromkeys(found))
    if not unique:
        raise PatchError("patch did not contain recognizable file paths")
    return unique


def validate_patch_scope(patch: str, allowed_files: Iterable[str]) -> tuple[str, ...]:
    allowed = {normalize_relative_path(path) for path in allowed_files}
    paths = patch_paths(patch)
    unauthorized = [path for path in paths if path not in allowed]
    if unauthorized:
        raise ScopeViolationError(unauthorized)
    return paths


def _normalize_candidate_content(path: str, content: str) -> str:
    """Normalize line endings and repair only provable Python transport escapes."""

    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if path.endswith(".py") and ("\\n" in normalized or '\\"' in normalized):
        try:
            ast.parse(normalized, filename=path)
        except SyntaxError:
            repaired = (
                normalized
                .replace("\\r\\n", "\n")
                .replace("\\n", "\n")
                .replace('\\"', '"')
            )
            try:
                ast.parse(repaired, filename=path)
            except SyntaxError:
                pass
            else:
                normalized = repaired
    return normalized


def replacement_diff(
    repo: Path,
    file_contents: Mapping[str, str],
    allowed_files: Iterable[str],
) -> str:
    """Convert bounded complete-file output into a normal unified diff."""
    if not file_contents:
        raise PatchError("worker returned no replacement files")
    allowed = {normalize_relative_path(path) for path in allowed_files}
    normalized: list[tuple[str, str]] = []
    for raw_path, content in file_contents.items():
        try:
            path = normalize_relative_path(raw_path)
        except ValueError as exc:
            raise PatchError(f"invalid replacement file path: {raw_path!r}") from exc
        if path not in allowed:
            raise ScopeViolationError((path,))
        if not isinstance(content, str):
            raise PatchError(f"replacement content for {path!r} must be text")
        normalized.append((path, _normalize_candidate_content(path, content)))

    chunks: list[str] = []
    root = repo.resolve()
    for path, new_content in normalized:
        target = root / Path(*path.split("/"))
        try:
            target.resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise PatchError(f"replacement target escapes repository: {path}") from exc
        if target.is_symlink():
            raise PatchError(f"replacement target must not be a symlink: {path}")
        if target.exists() and not target.is_file():
            raise PatchError(f"replacement target is not a file: {path}")
        old_content = ""
        if target.is_file():
            old_content = target.read_text(encoding="utf-8", errors="replace")
            old_content = old_content.replace("\r\n", "\n").replace("\r", "\n")
        if new_content and not new_content.endswith("\n"):
            new_content += "\n"
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        if old_lines == new_lines:
            raise PatchError(f"replacement content made no change to {path}")
        from_name = f"a/{path}" if target.is_file() else "/dev/null"
        to_name = f"b/{path}"
        body = "".join(unified_diff(
            old_lines,
            new_lines,
            fromfile=from_name,
            tofile=to_name,
            n=3,
            lineterm="\n",
        ))
        if not body:
            raise PatchError(f"could not build replacement diff for {path}")
        header = f"diff --git a/{path} b/{path}\n"
        if not target.is_file():
            header += "new file mode 100644\n"
        chunks.append(header + body)
    return "".join(chunks)


def append_diff(
    repo: Path,
    file_contents: Mapping[str, str],
    allowed_files: Iterable[str],
) -> str:
    """Append a bounded source snippet to one existing file.

    This is a recovery-only adapter for workers that return the new block rather
    than a unified diff. It rejects candidates that begin with an existing
    top-level declaration, which prevents a complete-file response from being
    mistaken for an append-only snippet.
    """

    if len(file_contents) != 1:
        raise PatchError("append output must contain exactly one file")
    raw_path, content = next(iter(file_contents.items()))
    try:
        path = normalize_relative_path(raw_path)
    except ValueError as exc:
        raise PatchError(f"invalid append file path: {raw_path!r}") from exc
    allowed = {normalize_relative_path(item) for item in allowed_files}
    if path not in allowed:
        raise ScopeViolationError((path,))
    if not isinstance(content, str):
        raise PatchError(f"append content for {path!r} must be text")

    root = repo.resolve()
    target = root / Path(*path.split("/"))
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise PatchError(f"append target escapes repository: {path}") from exc
    if target.is_symlink() or not target.is_file():
        raise PatchError(f"append target must be an existing regular file: {path}")

    old_content = target.read_text(encoding="utf-8", errors="replace")
    old_content = old_content.replace("\r\n", "\n").replace("\r", "\n")
    snippet = _normalize_candidate_content(path, content).strip("\n")
    if not snippet.strip():
        raise PatchError("append content must not be empty")
    if "diff --git " in snippet or snippet.startswith(("--- ", "+++ ", "@@ ")):
        raise PatchError("append content must be source, not a patch")

    if path.endswith(".py"):
        try:
            old_tree = ast.parse(old_content, filename=path)
            snippet_tree = ast.parse(snippet, filename=path)
        except SyntaxError as exc:
            raise PatchError(f"append source is not valid Python: {exc.msg}") from exc

        def signature(node: ast.AST) -> tuple[str, str | None]:
            name = getattr(node, "name", None)
            return type(node).__name__, name if isinstance(name, str) else None

        old_signatures = {signature(node) for node in old_tree.body}
        if snippet_tree.body and signature(snippet_tree.body[0]) in old_signatures:
            raise PatchError(
                "append candidate begins with an existing top-level declaration; "
                "it looks like complete-file content"
            )

    old_lines = old_content.splitlines(keepends=True)
    base = old_content
    if base and not base.endswith("\n"):
        base += "\n"
    if base and not base.endswith("\n\n"):
        base += "\n"
    new_content = base + snippet
    if not new_content.endswith("\n"):
        new_content += "\n"
    new_lines = new_content.splitlines(keepends=True)
    if old_lines == new_lines:
        raise PatchError(f"append content made no change to {path}")
    body = "".join(
        unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
            lineterm="\n",
        )
    )
    if not body:
        raise PatchError(f"could not build append diff for {path}")
    return f"diff --git a/{path} b/{path}\n{body}"


def normalize_single_file_hunk(patch: str) -> str:
    """Repair line counts in a one-file hunk emitted without file headers."""

    lines = patch.splitlines()
    if not lines or not lines[0].startswith("@@ "):
        return patch
    repaired: list[str] = []
    index = 0
    while index < len(lines):
        match = _HUNK_HEADER.match(lines[index])
        if match is None:
            return patch
        body_start = index + 1
        body_end = body_start
        while body_end < len(lines) and not lines[body_end].startswith("@@ "):
            body_end += 1
        body = lines[body_start:body_end]
        if any(
            line and line[0] not in {" ", "+", "-", "\\"}
            for line in body
        ):
            return patch
        old_count = sum(bool(line) and line[0] in {" ", "-"} for line in body)
        new_count = sum(bool(line) and line[0] in {" ", "+"} for line in body)
        suffix = match.group("suffix")
        repaired.append(
            "@@ -"
            + match.group(1)
            + f",{old_count} +"
            + match.group(3)
            + f",{new_count} @@"
            + suffix
        )
        repaired.extend(body)
        index = body_end
    return "\n".join(repaired) + ("\n" if patch.endswith("\n") else "")


def rebase_single_file_hunk(repo: Path, path: str, patch: str) -> str:
    """Rebase a headerless single-file hunk onto its unique old-line match."""

    normalized = normalize_single_file_hunk(patch)
    lines = normalized.splitlines()
    if not lines:
        return normalized
    match = _HUNK_HEADER.match(lines[0])
    if match is None:
        return normalized
    body = lines[1:]
    old_block = [
        line[1:]
        for line in body
        if line and line[0] in {" ", "-"}
    ]
    if not old_block:
        return normalized
    target = repo.resolve() / Path(*normalize_relative_path(path).split("/"))
    if not target.is_file() or target.is_symlink():
        return normalized
    old_lines = target.read_text(encoding="utf-8", errors="replace")
    old_lines = old_lines.replace("\r\n", "\n").replace("\r", "\n")
    source_lines = old_lines.splitlines(keepends=True)
    match_lines = old_lines.splitlines()
    matches = [
        index
        for index in range(len(match_lines) - len(old_block) + 1)
        if match_lines[index : index + len(old_block)] == old_block
    ]
    if len(matches) != 1:
        return normalized
    old_count = sum(bool(line) and line[0] in {" ", "-"} for line in body)
    new_count = sum(bool(line) and line[0] in {" ", "+"} for line in body)
    start = matches[0] + 1
    lines[0] = (
        f"@@ -{start},{old_count} +{start},{new_count} @@"
        + match.group("suffix")
    )
    return "\n".join(lines) + ("\n" if patch.endswith("\n") else "")


def append_patch_diff(
    repo: Path,
    patch: str,
    allowed_files: Iterable[str],
) -> str:
    """Recover an append-only source block from a noisy unified diff."""

    paths = patch_paths(patch)
    if len(paths) != 1:
        raise PatchError("append recovery requires exactly one patch path")
    additions = [
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    if not additions:
        raise PatchError("append recovery found no added source lines")
    return append_diff(
        repo,
        {paths[0]: "\n".join(additions) + "\n"},
        allowed_files,
    )


def append_hunk_diff(
    repo: Path,
    path: str,
    hunk: str,
    allowed_files: Iterable[str],
) -> str:
    """Recover new top-level source blocks from a malformed hunk response."""

    lines = hunk.splitlines()[1:]
    source: list[str] = []
    for line in lines:
        if line.startswith("+"):
            source.append(line[1:])
        elif line.startswith("-"):
            continue
        elif line.startswith("     "):
            source.append(line[1:])
        else:
            source.append(line)
    start = next(
        (
            index
            for index, line in enumerate(source)
            if line.strip()
            and not line[:1].isspace()
            and (
                line.startswith(("def ", "async def ", "class ", "@"))
            )
        ),
        None,
    )
    if start is None:
        raise PatchError("append recovery found no new top-level source block")
    return append_diff(
        repo,
        {path: "\n".join(source[start:]) + "\n"},
        allowed_files,
    )


def normalize_patch_transport(patch: str) -> str:
    """Repair bounded transport noise without changing patch intent.

    Local models sometimes decorate Git's optional ``index`` metadata with
    prose such as ``(old)``. Dropping only an invalid metadata line lets the
    real file headers and hunk remain independently checked by ``git apply``;
    valid index lines are preserved byte-for-byte.
    """

    # A text-only local worker can escape the entire diff as one JSON string.
    # Decode only when the escaped/newline form is clearly the transport
    # representation; do not rewrite a legitimate ``\\n`` inside a normal
    # source line.
    if patch.count("\n") <= 2 and ("\\n" in patch or "/n" in patch):
        patch = (
            patch.replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("/n", "\n")
        )
    lines = []
    for line in patch.splitlines():
        if line != line.lstrip() and line.lstrip().startswith("@@ "):
            line = line.lstrip()
        if line.startswith("index ") and not _VALID_INDEX_LINE.fullmatch(line):
            continue
        if line.startswith((" ", "+", "-")) and line.endswith("\\n"):
            line = line[:-2]
        lines.append(line)
    return "\n".join(lines) + ("\n" if patch.endswith("\n") else "")


def rebase_unified_patch(repo: Path, patch: str) -> str:
    """Repair hunk counts and unique old-line locations in a one-file diff."""

    paths = patch_paths(patch)
    if len(paths) != 1 or "@@ " not in patch:
        return patch
    path = paths[0]
    lines = patch.splitlines()
    hunk_indexes = [
        index for index, line in enumerate(lines) if line.startswith("@@ ")
    ]
    if len(hunk_indexes) == 1:
        start = hunk_indexes[0]
        end = len(lines)
        old_block: list[str] = []
        new_block: list[str] = []
        malformed = False
        hunk_lines = lines[start + 1 : end]
        while hunk_lines and hunk_lines[-1] == "":
            hunk_lines.pop()
        for line in hunk_lines:
            if not line:
                malformed = True
                break
            if line.startswith(" "):
                old_block.append(line[1:])
                new_block.append(line[1:])
            elif line.startswith("-"):
                old_block.append(line[1:])
            elif line.startswith("+") or line.startswith("\\"):
                if line.startswith("+"):
                    new_block.append(line[1:])
            else:
                malformed = True
                break
        target = repo.resolve() / Path(*path.split("/"))
        if not malformed and old_block and target.is_file() and not target.is_symlink():
            old_content = target.read_text(encoding="utf-8", errors="replace")
            old_content = old_content.replace("\r\n", "\n").replace("\r", "\n")
            source_lines = old_content.splitlines(keepends=True)
            match_lines = old_content.splitlines()
            matches = [
                index
                for index in range(len(match_lines) - len(old_block) + 1)
                if match_lines[index : index + len(old_block)] == old_block
            ]
            if len(matches) == 1:
                replacement = (
                    source_lines[: matches[0]]
                    + [line if line.endswith("\n") else line + "\n" for line in new_block]
                    + source_lines[matches[0] + len(old_block) :]
                )
                body = "".join(
                    unified_diff(
                        source_lines,
                        replacement,
                        fromfile=f"a/{path}",
                        tofile=f"b/{path}",
                        n=3,
                        lineterm="\n",
                    )
                )
                if body:
                    return f"diff --git a/{path} b/{path}\n{body}"
            # A small model may preserve the correct deleted line while
            # hallucinating stale surrounding context or line numbers. Permit
            # one deterministic recovery only when exactly one source line is
            # deleted, the replacement has additions, and that deleted line is
            # unique in the current file. Never fuzzy-match a multi-line block.
            deleted_lines = [
                line[1:] for line in hunk_lines if line.startswith("-")
            ]
            added_lines = [
                line[1:] for line in hunk_lines if line.startswith("+")
            ]
            if (
                not malformed
                and len(deleted_lines) == 1
                and added_lines
                and target.is_file()
            ):
                deleted = deleted_lines[0]
                unique_deleted = [
                    index
                    for index, line in enumerate(match_lines)
                    if line == deleted
                ]
                if len(unique_deleted) == 1:
                    replacement = (
                        source_lines[: unique_deleted[0]]
                        + [
                            line if line.endswith("\n") else line + "\n"
                            for line in added_lines
                        ]
                        + source_lines[unique_deleted[0] + 1 :]
                    )
                    body = "".join(
                        unified_diff(
                            source_lines,
                            replacement,
                            fromfile=f"a/{path}",
                            tofile=f"b/{path}",
                            n=3,
                            lineterm="\n",
                        )
                    )
                    if body:
                        return f"diff --git a/{path} b/{path}\n{body}"
    repaired: list[str] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("@@ "):
            repaired.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("@@ ") and not lines[end].startswith("diff --git "):
            end += 1
        hunk = "\n".join(lines[index:end]) + "\n"
        repaired.extend(rebase_single_file_hunk(repo, path, hunk).rstrip("\n").splitlines())
        index = end
    return "\n".join(repaired) + ("\n" if patch.endswith("\n") else "")


def _python_node_source_lines(
    source: str,
    node: ast.AST,
) -> list[str]:
    """Return a top-level Python node, including decorators, as source lines."""

    lines = source.splitlines(keepends=True)
    starts = [getattr(node, "lineno", 0)]
    starts.extend(
        getattr(decorator, "lineno", 0)
        for decorator in getattr(node, "decorator_list", ())
    )
    start = min(value for value in starts if value)
    end = getattr(node, "end_lineno", None) or start
    return lines[start - 1 : end]


def _top_level_shape_compatible(
    old_tree: ast.Module,
    new_tree: ast.Module,
) -> bool:
    """Allow bounded module setup while preserving existing definitions.

    A complete-file recovery may need to add an import and a module-level
    logger/constant around an existing function. Preserve the original
    top-level sequence and permit only setup-shaped additions; a new or
    missing function/class still rejects the candidate.
    """

    def signature(node: ast.AST) -> tuple[str, str | None]:
        name = getattr(node, "name", None)
        return (
            type(node).__name__,
            name if isinstance(name, str) else None,
        )

    old_signatures = [signature(node) for node in old_tree.body]
    new_signatures = [signature(node) for node in new_tree.body]
    if not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for node in old_tree.body
    ):
        return old_signatures == new_signatures
    matched_indexes: set[int] = set()
    cursor = 0
    for expected in old_signatures:
        try:
            index = new_signatures.index(expected, cursor)
        except ValueError:
            return False
        matched_indexes.add(index)
        cursor = index + 1

    allowed_setup = (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)
    return all(
        index in matched_indexes or isinstance(node, allowed_setup)
        for index, node in enumerate(new_tree.body)
    )


def _multi_ranged_python_replacement_diff(
    repo: Path,
    path: str,
    old_content: str,
    new_content: str,
    ranges: Sequence[tuple[str, int, int]],
) -> str:
    """Map one Python fence containing several declared definitions.

    The normal ranged protocol is intentionally one target at a time. A
    small model can nevertheless return two or more requested definitions in
    one ``files`` value. Match each candidate definition by its stable AST
    name, replace only its declared range, and reject any extra top-level
    executable code.
    """

    if not path.endswith(".py"):
        raise PatchError(
            "multiple ranged replacements require a Python target file"
        )
    normalized = new_content.replace("\r\n", "\n").replace("\r", "\n")
    if normalized and not normalized.endswith("\n"):
        normalized += "\n"
    try:
        old_tree = ast.parse(old_content, filename=path)
        new_tree = ast.parse(normalized, filename=path)
    except SyntaxError as exc:
        raise PatchError(
            f"multi-ranged replacement is not valid Python: {exc.msg}"
        ) from exc

    old_lines = old_content.splitlines(keepends=True)
    new_lines = normalized.splitlines(keepends=True)
    target_nodes: list[tuple[int, int, ast.AST]] = []
    expected_keys: list[tuple[str, str | None]] = []
    for _context_path, start, end in ranges:
        if start < 1 or end < start or end > len(old_lines):
            raise PatchError(f"ranged replacement lines are outside {path!r}")
        nodes = [
            node
            for node in old_tree.body
            if getattr(node, "lineno", 0) >= start
            and (getattr(node, "end_lineno", 0) or getattr(node, "lineno", 0)) <= end
        ]
        if len(nodes) != 1:
            raise PatchError(
                f"ranged replacement {path}:{start}-{end} must contain one top-level definition"
            )
        node = nodes[0]
        target_nodes.append((start, end, node))
        expected_keys.append(
            (
                type(node).__name__,
                getattr(node, "name", None)
                if isinstance(getattr(node, "name", None), str)
                else None,
            )
        )

    candidate_nodes = list(new_tree.body)
    candidate_by_key: dict[tuple[str, str | None], ast.AST] = {}
    for node in candidate_nodes:
        key = (
            type(node).__name__,
            getattr(node, "name", None)
            if isinstance(getattr(node, "name", None), str)
            else None,
        )
        if key in candidate_by_key:
            raise PatchError("multi-ranged replacement has duplicate definitions")
        candidate_by_key[key] = node
    expected_set = set(expected_keys)
    if set(candidate_by_key) != expected_set:
        raise PatchError(
            "multi-ranged replacement must contain exactly the declared definitions"
        )

    replacement = list(old_lines)
    replacements: list[tuple[int, int, list[str]]] = []
    for (start, end, _old_node), key in zip(target_nodes, expected_keys):
        candidate = candidate_by_key[key]
        source_lines = _python_node_source_lines(normalized, candidate)
        if not source_lines:
            raise PatchError("multi-ranged replacement definition is empty")
        replacements.append((start - 1, end, source_lines))
    for start, end, source_lines in sorted(replacements, reverse=True):
        replacement[start:end] = source_lines
    if replacement == old_lines:
        raise PatchError(f"ranged replacement made no change to {path}")

    body = "".join(
        unified_diff(
            old_lines,
            replacement,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
            lineterm="\n",
        )
    )
    if not body:
        raise PatchError(f"could not build ranged replacement diff for {path}")
    return f"diff --git a/{path} b/{path}\n{body}"


def ranged_replacement_diff(
    repo: Path,
    file_contents: Mapping[str, str],
    allowed_files: Iterable[str],
    context_specs: Sequence[str],
    *,
    context_mode: str = "replace",
) -> str:
    """Convert one or more AST-shaped target-range snippets into a checked diff.

    Small local models sometimes return the target definition as a ``files``
    value even when they received only a ranged context. Accept that form only
    when it can be safely mapped back to exactly one declared range and the
    replacement has the same top-level syntax shape as the original range. An
    explicit ``insert_after`` mode extracts one or more new test definitions
    without replacing the existing test in the context range.
    """
    ranged_specs = []
    for spec in context_specs:
        path, start, end = context_path_and_range(spec)
        if start is not None:
            if end is None:
                end = start
            ranged_specs.append((path, start, end))
    if len(file_contents) != 1:
        raise PatchError(
            "ranged file output must contain exactly one replacement file"
        )

    raw_path, new_content = next(iter(file_contents.items()))
    try:
        path = normalize_relative_path(raw_path)
    except ValueError as exc:
        raise PatchError(f"invalid ranged replacement path: {raw_path!r}") from exc
    if path not in {normalize_relative_path(item) for item in allowed_files}:
        raise ScopeViolationError((path,))
    matching_specs = [
        (context_path, start, end)
        for context_path, start, end in ranged_specs
        if normalize_relative_path(context_path) == path
    ]
    unique_matching_specs = list(dict.fromkeys(matching_specs))
    if not unique_matching_specs:
        raise PatchError(
            "ranged file output must match a declared line range for its replacement file"
        )
    if len(unique_matching_specs) > 1:
        if context_mode != "replace":
            raise PatchError(
                "multiple ranged file outputs are supported only in replace mode"
            )
        context_path = unique_matching_specs[0][0]
        if not path.endswith(".py"):
            raise PatchError(
                "multiple ranged file outputs require a Python replacement file"
            )
        if not isinstance(new_content, str):
            raise PatchError(f"ranged replacement content for {path!r} must be text")
        context_ranges = [
            (item_path, item_start, item_end)
            for item_path, item_start, item_end in unique_matching_specs
        ]
        # The target is validated below before the single-range path reads it;
        # do the same bounded path checks here before mapping the definitions.
        root = repo.resolve()
        target = root / Path(*path.split("/"))
        try:
            target.resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise PatchError(
                f"ranged replacement target escapes repository: {path}"
            ) from exc
        if target.is_symlink():
            raise PatchError(f"ranged replacement target must not be a symlink: {path}")
        if not target.is_file():
            raise PatchError(f"ranged replacement target must be an existing file: {path}")
        old_content = target.read_text(encoding="utf-8", errors="replace")
        old_content = old_content.replace("\r\n", "\n").replace("\r", "\n")
        return _multi_ranged_python_replacement_diff(
            repo,
            path,
            old_content,
            new_content,
            context_ranges,
        )
    context_path, start, end = unique_matching_specs[0]
    if path != context_path:
        raise PatchError(
            f"ranged replacement path {path!r} does not match context {context_path!r}"
        )
    if not isinstance(new_content, str):
        raise PatchError(f"ranged replacement content for {path!r} must be text")

    root = repo.resolve()
    target = root / Path(*path.split("/"))
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise PatchError(f"ranged replacement target escapes repository: {path}") from exc
    if target.is_symlink():
        raise PatchError(f"ranged replacement target must not be a symlink: {path}")
    if not target.is_file():
        raise PatchError(f"ranged replacement target must be an existing file: {path}")

    old_content = target.read_text(encoding="utf-8", errors="replace")
    old_content = old_content.replace("\r\n", "\n").replace("\r", "\n")
    old_lines = old_content.splitlines(keepends=True)
    if start < 1 or end < start or end > len(old_lines):
        raise PatchError(f"ranged replacement lines are outside {path!r}")
    old_segment = "".join(old_lines[start - 1 : end])
    normalized = new_content.replace("\r\n", "\n").replace("\r", "\n")
    if normalized and not normalized.endswith("\n"):
        normalized += "\n"
    if not normalized.strip():
        raise PatchError("ranged replacement content must not be empty")

    try:
        old_tree = ast.parse(old_segment, filename=path)
    except SyntaxError as exc:
        raise PatchError(f"existing ranged content is not valid Python: {exc.msg}") from exc

    new_tree = None
    parse_candidates = [normalized]
    # A local model can double-escape the JSON string containing a Python
    # snippet, leaving literal ``\\n`` or ``\\"`` transport escapes in the
    # decoded file content. Try a syntax-only transport normalization after
    # the original candidate fails; never rewrite code that already parses.
    if "\\n" in normalized or '\\"' in normalized:
        repaired = normalized.replace("\\n", "\n").replace('\\"', '"')
        if repaired != normalized:
            parse_candidates.append(repaired)
    parse_error: SyntaxError | None = None
    for candidate in parse_candidates:
        try:
            new_tree = ast.parse(candidate, filename=path)
            normalized = candidate
            break
        except SyntaxError as exc:
            parse_error = exc
    if new_tree is None:
        assert parse_error is not None
        raise PatchError(
            f"ranged replacement is not valid Python: {parse_error.msg}"
        ) from parse_error

    def node_shape(tree: ast.Module) -> list[tuple[str, str | None]]:
        shape: list[tuple[str, str | None]] = []
        for node in tree.body:
            name = getattr(node, "name", None)
            shape.append((type(node).__name__, name if isinstance(name, str) else None))
        return shape

    new_lines = normalized.splitlines(keepends=True)
    old_shape = node_shape(old_tree)
    new_shape = node_shape(new_tree)
    if context_mode == "insert_after":
        if not path.startswith("tests/"):
            raise PatchError("insert_after ranged output is restricted to test files")
        if len(old_shape) != 1 or old_shape[0][0] not in {"FunctionDef", "AsyncFunctionDef"}:
            raise PatchError("insert_after range must contain one existing test function")
        existing = next(
            (node for node in new_tree.body if getattr(node, "name", None) == old_shape[0][1]),
            None,
        )
        if existing is not None:
            old_node = old_tree.body[0]
            if ast.dump(existing, include_attributes=False) != ast.dump(old_node, include_attributes=False):
                raise PatchError("insert_after output changed the existing test")
        if existing is not None:
            try:
                complete_old_tree = ast.parse(old_content, filename=path)
            except SyntaxError as exc:
                raise PatchError(
                    f"insert_after source is not valid Python: {exc.msg}"
                ) from exc
            original_names = {
                getattr(node, "name", None)
                for node in complete_old_tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            candidate_names = {
                getattr(node, "name", None)
                for node in new_tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if any(
                name in original_names and name != old_shape[0][1]
                for name in candidate_names
            ):
                raise PatchError(
                    "insert_after candidate appears to be complete-file content"
                )
        new_tests = [
            node for node in new_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and getattr(node, "name", "") != old_shape[0][1]
        ]
        if not new_tests:
            raise PatchError("insert_after output must contain a new test function")
        if any(
            not getattr(new_test, "name", "").startswith("test_")
            for new_test in new_tests
        ):
            raise PatchError("insert_after output must define only test functions")
        if any(
            not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef))
            for node in new_tree.body
        ):
            raise PatchError("insert_after output contains unsupported top-level syntax")
        inserted_lines: list[str] = []
        for index, new_test in enumerate(new_tests):
            if index:
                inserted_lines.append("\n")
            test_start = min(
                [decorator.lineno for decorator in new_test.decorator_list]
                + [new_test.lineno]
            )
            test_end = new_test.end_lineno or new_test.lineno
            inserted_lines.extend(new_lines[test_start - 1 : test_end])
        if not inserted_lines:
            raise PatchError("insert_after output did not contain test source")
        replacement = old_lines[:end] + inserted_lines + old_lines[end:]
    else:
        if old_shape != new_shape:
            raise PatchError("ranged replacement changed the target's top-level syntax shape")
        if old_lines[start - 1 : end] == new_lines:
            raise PatchError(f"ranged replacement made no change to {path}")
        replacement = old_lines[: start - 1] + new_lines + old_lines[end:]
    body = "".join(
        unified_diff(
            old_lines,
            replacement,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
            lineterm="\n",
        )
    )
    if not body:
        raise PatchError(f"could not build ranged replacement diff for {path}")
    return f"diff --git a/{path} b/{path}\n{body}"


def ranged_full_file_diff(
    repo: Path,
    file_contents: Mapping[str, str],
    allowed_files: Iterable[str],
    context_specs: Sequence[str],
    *,
    context_mode: str = "replace",
) -> str:
    """Safely convert a complete-file candidate for a ranged task.

    The compact ranged protocol asks the worker for only the target definition,
    but Codex/Ollama workers sometimes return the whole file. Accept that
    higher-token fallback only when a line diff proves that every change is
    inside the declared range (or is one or more new tests immediately after
    an ``insert_after`` target). This preserves the range as a write boundary.
    """

    ranged_specs = []
    for spec in context_specs:
        path, start, end = context_path_and_range(spec)
        if start is not None:
            ranged_specs.append((path, start, end if end is not None else start))
    if len(file_contents) != 1:
        raise PatchError(
            "ranged full-file output must contain exactly one replacement file"
        )

    raw_path, new_content = next(iter(file_contents.items()))
    try:
        path = normalize_relative_path(raw_path)
    except ValueError as exc:
        raise PatchError(f"invalid ranged replacement path: {raw_path!r}") from exc
    allowed = {normalize_relative_path(item) for item in allowed_files}
    if path not in allowed:
        raise ScopeViolationError((path,))
    matches = [
        item for item in ranged_specs
        if normalize_relative_path(item[0]) == path
    ]
    if not matches:
        raise PatchError(
            "ranged full-file output must match a declared line range for its replacement file"
        )
    if context_mode == "insert_after" and len(matches) != 1:
        raise PatchError(
            "insert_after full-file output must match exactly one line range"
        )
    ranges = list(dict.fromkeys(matches))
    _, start, end = ranges[0]
    if not isinstance(new_content, str):
        raise PatchError(f"ranged replacement content for {path!r} must be text")

    root = repo.resolve()
    target = root / Path(*path.split("/"))
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise PatchError(f"ranged replacement target escapes repository: {path}") from exc
    if target.is_symlink() or not target.is_file():
        raise PatchError(
            f"ranged replacement target must be an existing regular file: {path}"
        )

    old_content = target.read_text(encoding="utf-8", errors="replace")
    old_content = old_content.replace("\r\n", "\n").replace("\r", "\n")
    old_lines = old_content.splitlines(keepends=True)
    if start < 1 or end < start or end > len(old_lines):
        raise PatchError(f"ranged replacement lines are outside {path!r}")

    normalized = new_content.replace("\r\n", "\n").replace("\r", "\n")
    if normalized and not normalized.endswith("\n"):
        normalized += "\n"
    new_lines = normalized.splitlines(keepends=True)
    if old_lines == new_lines:
        raise PatchError(f"ranged full-file replacement made no change to {path}")

    if path.endswith(".py"):
        # Only try transport repair after the complete candidate fails as-is.
        parse_candidates = [normalized]
        if "\\n" in normalized or '\\"' in normalized:
            repaired = normalized.replace("\\n", "\n").replace('\\"', '"')
            if repaired != normalized:
                parse_candidates.append(repaired)
        parse_error: SyntaxError | None = None
        for candidate in parse_candidates:
            try:
                ast.parse(candidate, filename=path)
                normalized = candidate
                new_lines = normalized.splitlines(keepends=True)
                break
            except SyntaxError as exc:
                parse_error = exc
        else:
            assert parse_error is not None
            raise PatchError(
                f"ranged full-file replacement is not valid Python: {parse_error.msg}"
            ) from parse_error

        try:
            old_tree = ast.parse(old_content, filename=path)
            new_tree = ast.parse(normalized, filename=path)
        except SyntaxError as exc:
            raise PatchError(
                f"ranged full-file replacement is not valid Python: {exc.msg}"
            ) from exc

        if (
            context_mode != "insert_after"
            and not _top_level_shape_compatible(old_tree, new_tree)
        ):
            raise PatchError(
                "ranged full-file replacement changed the top-level Python shape"
            )

    changed = [
        opcode
        for opcode in SequenceMatcher(
            a=old_lines, b=new_lines, autojunk=False
        ).get_opcodes()
        if opcode[0] != "equal"
    ]
    old_start = start - 1
    old_end = end
    if context_mode == "insert_after":
        if not path.startswith("tests/"):
            raise PatchError("insert_after ranged output is restricted to test files")
        if len(changed) != 1 or changed[0][0] != "insert":
            raise PatchError(
                "ranged full-file insert_after output must contain one insertion block only"
            )
        old_segment = "".join(old_lines[old_start:old_end])
        try:
            old_target_tree = ast.parse(old_segment, filename=path)
            old_tree = ast.parse(old_content, filename=path)
            new_tree = ast.parse(normalized, filename=path)
        except SyntaxError as exc:
            raise PatchError(
                f"ranged full-file insert_after is not valid Python: {exc.msg}"
            ) from exc
        if (
            len(old_target_tree.body) != 1
            or not isinstance(
                old_target_tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)
            )
        ):
            raise PatchError("insert_after range must contain one existing test function")
        old_body = [ast.dump(node, include_attributes=False) for node in old_tree.body]
        new_body = [ast.dump(node, include_attributes=False) for node in new_tree.body]
        body_changes = [
            opcode
            for opcode in SequenceMatcher(
                a=old_body, b=new_body, autojunk=False
            ).get_opcodes()
            if opcode[0] != "equal"
        ]
        if len(body_changes) != 1 or body_changes[0][0] != "insert":
            raise PatchError(
                "ranged full-file insert_after must add one or more top-level tests"
            )
        _body_tag, _bi1, _bi2, bj1, bj2 = body_changes[0]
        inserted_nodes = new_tree.body[bj1:bj2]
        target_name = getattr(old_target_tree.body[0], "name", "")
        target_indexes = [
            index
            for index, node in enumerate(old_tree.body)
            if getattr(node, "name", None) == target_name
        ]
        if (
            not inserted_nodes
            or any(
                not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                or not getattr(node, "name", "").startswith("test_")
                for node in inserted_nodes
            )
            or len(target_indexes) != 1
            or bj1 != target_indexes[0] + 1
        ):
            raise PatchError(
                "ranged full-file insert_after must add one or more top-level tests"
            )
    else:
        allowed_ranges = [(item_start - 1, item_end) for _, item_start, item_end in ranges]
        for _tag, i1, i2, _j1, _j2 in changed:
            if not any(
                i1 >= range_start
                and i2 <= range_end
                and not (i1 == i2 and i1 >= range_end)
                for range_start, range_end in allowed_ranges
            ):
                raise PatchError(
                    "ranged full-file replacement changed lines outside the declared target"
                )

    body = "".join(
        unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
            lineterm="\n",
        )
    )
    if not body:
        raise PatchError(f"could not build ranged full-file diff for {path}")
    return f"diff --git a/{path} b/{path}\n{body}"


def _run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    try:
        return subprocess.run(
            command,
            cwd=repo,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command,
            124,
            stdout=str(exc.stdout or ""),
            stderr=(str(exc.stderr or "") + "\ngit command timed out"),
        )


def apply_patch(repo: Path, patch: str) -> None:
    patch_paths(patch)
    patch_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".patch",
            delete=False,
        ) as handle:
            handle.write(patch.encode("utf-8"))
            patch_file = Path(handle.name)
        check = _run_git(
            repo,
            ["apply", "--recount", "--check", "--whitespace=nowarn", str(patch_file)],
        )
        if check.returncode != 0:
            detail = (check.stderr or check.stdout).strip()
            raise PatchError(f"git apply --check failed: {detail[:1000]}")
        applied = _run_git(
            repo,
            ["apply", "--recount", "--whitespace=nowarn", str(patch_file)],
        )
        if applied.returncode != 0:
            detail = (applied.stderr or applied.stdout).strip()
            raise PatchError(f"git apply failed: {detail[:1000]}")
    finally:
        if patch_file is not None:
            patch_file.unlink(missing_ok=True)


def check_patch(repo: Path, patch: str) -> None:
    """Validate a patch against a repository without changing its files."""

    patch_paths(patch)
    patch_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".patch",
            delete=False,
        ) as handle:
            handle.write(patch.encode("utf-8"))
            patch_file = Path(handle.name)
        check = _run_git(
            repo,
            ["apply", "--recount", "--check", "--whitespace=nowarn", str(patch_file)],
        )
        if check.returncode != 0:
            detail = (check.stderr or check.stdout).strip()
            raise PatchError(f"git apply --check failed: {detail[:1000]}")
    finally:
        if patch_file is not None:
            patch_file.unlink(missing_ok=True)


def _intent_to_add_untracked(repo: Path) -> None:
    status = _run_git(repo, ["status", "--porcelain", "--untracked-files=all"])
    if status.returncode != 0:
        raise PatchError(f"git status failed: {status.stderr.strip()[:500]}")
    paths: list[str] = []
    for line in status.stdout.splitlines():
        if line.startswith("?? "):
            paths.append(line[3:].strip())
    if paths:
        result = _run_git(repo, ["add", "-N", "--", *paths])
        if result.returncode != 0:
            raise PatchError(f"git add -N failed: {result.stderr.strip()[:500]}")


def changed_files(repo: Path) -> tuple[str, ...]:
    _intent_to_add_untracked(repo)
    result = _run_git(repo, ["diff", "--name-only", "HEAD"])
    if result.returncode != 0:
        raise PatchError(f"git diff failed: {result.stderr.strip()[:500]}")
    return tuple(
        normalize_relative_path(line.strip())
        for line in result.stdout.splitlines()
        if line.strip()
    )


def capture_diff(repo: Path) -> str:
    _intent_to_add_untracked(repo)
    result = _run_git(repo, ["diff", "--binary", "HEAD"])
    if result.returncode != 0:
        raise PatchError(f"git diff failed: {result.stderr.strip()[:500]}")
    return result.stdout


def worktree_status(repo: Path) -> tuple[str, ...]:
    result = _run_git(repo, ["status", "--porcelain", "--untracked-files=all"])
    if result.returncode != 0:
        raise PatchError(f"git status failed: {result.stderr.strip()[:500]}")
    return tuple(line for line in result.stdout.splitlines() if line.strip())
