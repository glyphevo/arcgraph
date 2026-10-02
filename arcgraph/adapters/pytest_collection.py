"""Static pytest collection patterns; never import project configuration code."""

from __future__ import annotations

import ast
import configparser
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
import shlex
import tomllib

from arcgraph.core.schemas import FileRecord


@dataclass(frozen=True)
class PytestCollection:
    python_files: tuple[str, ...] = ("test_*.py", "*_test.py")
    python_functions: tuple[str, ...] = ("test",)
    python_classes: tuple[str, ...] = ("Test",)

    @classmethod
    def from_files(cls, files: list[FileRecord]) -> PytestCollection:
        if not files:
            return cls()
        file = files[0]
        root = Path(file.abs_path)
        for _ in PurePosixPath(file.path).parts:
            root = root.parent
        return cls.from_root(root)

    @classmethod
    def from_root(cls, root: Path) -> PytestCollection:
        for name, section in (
            ("pytest.ini", "pytest"),
            (".pytest.ini", "pytest"),
            ("pyproject.toml", ""),
            ("tox.ini", "pytest"),
            ("setup.cfg", "tool:pytest"),
        ):
            path = root / name
            if not path.is_file():
                continue
            try:
                if name == "pyproject.toml":
                    values = (
                        tomllib.loads(path.read_text(encoding="utf-8"))
                        .get("tool", {})
                        .get("pytest", {})
                        .get("ini_options")
                    )
                    if values is None:
                        continue
                else:
                    config = configparser.ConfigParser(interpolation=None)
                    config.read_string(path.read_text(encoding="utf-8"))
                    if not config.has_section(section):
                        if name in {"pytest.ini", ".pytest.ini"}:
                            return cls()
                        continue
                    values = config[section]
                defaults = cls()
                patterns = {}
                for key in ("python_files", "python_functions", "python_classes"):
                    value = values.get(key, getattr(defaults, key))
                    patterns[key] = tuple(
                        shlex.split(value) if isinstance(value, str) else value
                    )
                if any(
                    not isinstance(p, str) for group in patterns.values() for p in group
                ):
                    return cls((), (), ())
                return cls(**patterns)
            except (OSError, ValueError, TypeError, AttributeError, configparser.Error):
                # Invalid static configuration provides no collection evidence.
                return cls((), (), ())
        return cls()

    def file_matches(self, path: str) -> bool:
        return any(fnmatchcase(PurePosixPath(path).name, p) for p in self.python_files)

    @staticmethod
    def _name_matches(name: str, patterns: tuple[str, ...]) -> bool:
        return any(name.startswith(p) or fnmatchcase(name, p) for p in patterns)

    def is_test(
        self,
        file: FileRecord,
        stmt: ast.FunctionDef | ast.AsyncFunctionDef,
        owner: str = "",
    ) -> bool:
        return (
            self.file_matches(file.path)
            and self._name_matches(stmt.name, self.python_functions)
            and (not owner or self._name_matches(owner, self.python_classes))
        )
