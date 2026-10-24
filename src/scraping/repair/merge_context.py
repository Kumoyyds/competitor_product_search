"""Bounded merge excerpts selected by static output-field dependencies.

Only source is inspected: candidate parsers are never imported or executed.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from difflib import SequenceMatcher

_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


class _Dependencies:
    def __init__(self, source: str):
        self.tree = ast.parse(source)
        self.parents = {child: node for node in ast.walk(self.tree) for child in ast.iter_child_nodes(node)}
        self.bindings = defaultdict(list)
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.bindings[(self.scope(node), node.name)].append(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                            self.bindings[(self.scope(node), name.id)].append(node)
            elif isinstance(node, ast.arg):
                # Arguments shadow names in outer scopes, including helper names.
                self.bindings[(self.scope(node), node.arg)].append(node)

    def scope(self, node):
        parent = self.parents.get(node)
        while parent is not None and not isinstance(parent, _SCOPES):
            parent = self.parents.get(parent)
        return parent

    def resolve(self, name):
        scope = self.scope(name)
        while scope is not None:
            found = self.bindings.get((scope, name.id))
            if found:
                return found
            scope = self.scope(scope)
        return []

    def field_roots(self, field):
        roots = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Dict):
                roots.extend(value for key, value in zip(node.keys, node.values)
                             if isinstance(key, ast.Constant) and key.value == field)
            elif isinstance(node, ast.keyword) and node.arg == field:
                roots.append(node.value)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == field:
                        roots.append(node)
                    elif (isinstance(target, ast.Subscript)
                          and isinstance(target.slice, ast.Constant) and target.slice.value == field):
                        roots.append(node)
        return roots

    def lines_for(self, field):
        """Trace values through locals, aliases, helper calls and helper returns."""
        pending = self.field_roots(field)
        visited, relevant, anchors = set(), set(), set()
        while pending:
            node = pending.pop()
            if node in visited:
                continue
            visited.add(node)
            if isinstance(node, ast.arg):
                continue
            start = getattr(node, "lineno", 1) - 1
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Inspect the helper's return dependencies, rather than dumping
                # its whole body (which may itself contain unrelated early edits).
                relevant.add(start)
                anchors.add(start)
                pending.extend(child for child in ast.walk(node)
                               if isinstance(child, ast.Return) and self.scope(child) is node)
                pending.extend(node.args.defaults)
                pending.extend(value for value in node.args.kw_defaults if value is not None)
                continue
            end = getattr(node, "end_lineno", start + 1)
            relevant.update(range(start, end))
            anchors.add(start)
            pending.extend(definition for name in ast.walk(node)
                           if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Load)
                           for definition in self.resolve(name))
            # Preserve conditions controlling a selected return/assignment.
            parent = self.parents.get(node)
            while parent is not None and not isinstance(parent, _SCOPES):
                if isinstance(parent, (ast.If, ast.IfExp, ast.While)):
                    pending.append(parent.test)
                elif isinstance(parent, (ast.For, ast.AsyncFor)):
                    pending.append(parent.iter)
                parent = self.parents.get(parent)
        return relevant, anchors


def focused_merge_source(source: str, other: str, fields: set[str], *, cap: int) -> str:
    """Budget only after tracing field values to actual extraction statements."""
    lines, other_lines = source.splitlines(), other.splitlines()
    if not lines or not fields:
        return "(no field-specific source)"[:cap]
    changed = set()
    for kind, start, end, _, _ in SequenceMatcher(None, lines, other_lines, autojunk=False).get_opcodes():
        if kind != "equal":
            changed.update(range(start, end))
    try:
        dependencies = _Dependencies(source)
    except SyntaxError:
        dependencies = None
    per_field = max(0, (cap - 2 * len(fields)) // len(fields))
    sections = []
    for field in sorted(fields):
        pattern = re.compile(r"\b" + re.escape(field) + r"\b")
        relevant, anchors = (dependencies.lines_for(field) if dependencies else (set(), set()))
        if not relevant:
            # Invalid/generated nonstandard syntax still gets literal evidence.
            relevant = {index for index, line in enumerate(lines) if pattern.search(line)}
        if not relevant:
            sections.append(f"{field}: no statically traceable field source"[:per_field])
            continue
        # Changed dependency statements outrank unchanged output assignments.
        # Headers and alias links then explain how that implementation is used.
        ordered = sorted(relevant, key=lambda index: (index not in changed, index not in anchors, index))
        selected, used = [], len(field) + 2
        for index in ordered:
            rendered = f"  L{index + 1}: {lines[index]}"
            if used + len(rendered) + 1 > per_field:
                if not selected:
                    available = max(0, per_field - used - 1)
                    match = pattern.search(rendered)
                    offset = max(0, match.start() - available // 4) if match else 0
                    selected.append((index, rendered[offset:offset + available]))
                    used += len(selected[-1][1]) + 1
                continue
            selected.append((index, rendered))
            used += len(rendered) + 1
        sections.append((field + ":\n" + "\n".join(line for _, line in sorted(selected)))[:per_field])
    return "\n\n".join(sections)[:cap]
