#!/usr/bin/env python3
"""Valida versionamento e changelog obrigatorios em hooks de Git."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
CHANGELOG_FILE = ROOT / "CHANGELOG.md"


def run_git(*args: str) -> str:
    """Executa comando git na raiz do projeto e retorna stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Falha ao executar git")
    return result.stdout.strip()


def fail(message: str) -> int:
    """Exibe mensagem de falha padronizada e retorna codigo de erro."""
    print("[release-check] " + message, file=sys.stderr)
    return 1


def read_version() -> str:
    """Le versao do arquivo VERSION e valida formato semver."""
    if not VERSION_FILE.exists():
        raise FileNotFoundError("Arquivo VERSION nao encontrado.")
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("VERSION deve estar no formato semver: X.Y.Z")
    return version


def changelog_has_version(version: str) -> bool:
    """Verifica se o CHANGELOG possui cabecalho da versao atual."""
    if not CHANGELOG_FILE.exists():
        return False
    content = CHANGELOG_FILE.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$",
        re.MULTILINE,
    )
    return bool(pattern.search(content))


def staged_files() -> set[str]:
    """Lista arquivos atualmente adicionados na index do git."""
    output = run_git("diff", "--cached", "--name-only")
    files = [line.strip() for line in output.splitlines() if line.strip()]
    return set(files)


def head_commit_files() -> set[str]:
    """Lista arquivos modificados no commit HEAD."""
    output = run_git("show", "--name-only", "--pretty=format:", "HEAD")
    files = [line.strip() for line in output.splitlines() if line.strip()]
    return set(files)


def check_common() -> tuple[bool, str]:
    """Executa validacoes comuns para pre-commit e pre-push."""
    try:
        version = read_version()
    except (FileNotFoundError, ValueError) as exc:
        return False, str(exc)

    if not changelog_has_version(version):
        return (
            False,
            "CHANGELOG.md deve conter cabecalho no formato: "
            f"## [{version}] - AAAA-MM-DD",
        )

    return True, ""


def run_pre_commit() -> int:
    """Impede commit quando VERSION e CHANGELOG nao forem atualizados."""
    ok, message = check_common()
    if not ok:
        return fail(message)

    staged = staged_files()
    if not staged:
        return 0

    required = {"VERSION", "CHANGELOG.md"}
    missing = required - staged
    if missing:
        missing_sorted = ", ".join(sorted(missing))
        return fail(
            "Commit bloqueado. Atualize e adicione na index: " + missing_sorted
        )
    return 0


def run_pre_push() -> int:
    """Impede push quando o ultimo commit nao traz versao e changelog."""
    ok, message = check_common()
    if not ok:
        return fail(message)

    try:
        changed_in_head = head_commit_files()
    except RuntimeError as exc:
        return fail(str(exc))

    required = {"VERSION", "CHANGELOG.md"}
    missing = required - changed_in_head
    if missing:
        missing_sorted = ", ".join(sorted(missing))
        return fail(
            "Push bloqueado. O ultimo commit deve incluir: " + missing_sorted
        )
    return 0


def main() -> int:
    """Ponto de entrada do script de validacao para hooks."""
    if len(sys.argv) != 2 or sys.argv[1] not in {"pre-commit", "pre-push"}:
        print(
            "Uso: validate_release.py [pre-commit|pre-push]",
            file=sys.stderr,
        )
        return 2

    mode = sys.argv[1]
    if mode == "pre-commit":
        return run_pre_commit()
    return run_pre_push()


if __name__ == "__main__":
    raise SystemExit(main())
