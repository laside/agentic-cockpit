#!/usr/bin/env python3
"""Garde de confidentialité : contrôle des fichiers suivis et des commits Git."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ZERO_SHA = "0" * 40
# Marqueur de fin de ligne qui exempte une fixture de test des contrôles GÉNÉRIQUES (jamais de la liste privée).
ALLOW_MARKER = "privacy-check: allow"
EXEMPT_IPV4 = "192.168.0.10"
ALLOWED_EMAIL_DOMAINS = frozenset(
    {
        "example.com",
        "example.org",
        "localhost",
        "users.noreply.github.com",
        "anthropic.com",
    }
)

# Adresses exactes publiques (identité committer des squash faits par GitHub).
ALLOWED_EMAIL_ADDRESSES = frozenset({"noreply" + "@" + "github.com"})

IPV4_RE = re.compile(
    r"(?<!\d)(?<!\d\.)(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\.\d)(?!\d)"
)
EMAIL_RE = re.compile(
    r"\b([A-Za-z0-9._%+-]+)@(localhost|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b"
)


def _is_private_rfc1918(octets: tuple[int, int, int, int]) -> bool:
    a, b, c, d = octets
    if any(o < 0 or o > 255 for o in (a, b, c, d)):
        return False
    if f"{a}.{b}.{c}.{d}" == EXEMPT_IPV4:
        return False
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    return False


def _default_deny_file() -> Path:
    config_dir = os.environ.get("COCKPIT_CONFIG_DIR")
    if config_dir:
        base = Path(config_dir).expanduser()
    else:
        base = Path("~/.config/agentic-cockpit").expanduser()
    return base / "forbidden-words.txt"


def _load_deny_patterns(deny_file: Path) -> list[tuple[str, re.Pattern[str]]]:
    if not deny_file.is_file():
        print(
            f"Avertissement : fichier de mots interdits absent ({deny_file}), "
            "seuls les contrôles génériques sont actifs.",
            file=sys.stderr,
        )
        return []
    patterns: list[tuple[str, re.Pattern[str]]] = []
    try:
        content = deny_file.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"Avertissement : lecture impossible de {deny_file} ({exc}).",
            file=sys.stderr,
        )
        return []
    for idx, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append((f"mot-interdit#{idx}", re.compile(line, re.IGNORECASE)))
    return patterns


def _check_text_line(
    line: str,
    deny_patterns: list[tuple[str, re.Pattern[str]]],
) -> list[str]:
    motifs: list[str] = []
    generic = ALLOW_MARKER not in line

    for m in (IPV4_RE.finditer(line) if generic else ()):
        octets = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        if _is_private_rfc1918(octets):
            motifs.append("ipv4-rfc1918")

    for m in (EMAIL_RE.finditer(line) if generic else ()):
        domain = m.group(2).lower()
        if m.group(0).lower() in ALLOWED_EMAIL_ADDRESSES:
            continue
        if domain not in ALLOWED_EMAIL_DOMAINS:
            motifs.append("email-non-autorise")

    for label, pattern in deny_patterns:
        if pattern.search(line):
            motifs.append(label)

    return motifs


def _check_tracked_files(
    repo: Path,
    deny_patterns: list[tuple[str, re.Pattern[str]]],
) -> list[str]:
    findings: list[str] = []
    proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return findings

    paths = [raw_rel for raw_rel in proc.stdout.split(b"\x00") if raw_rel]
    for index, raw_rel in enumerate(paths, start=1):
        rel_path = raw_rel.decode("utf-8", errors="surrogateescape")

        path_motifs = _check_text_line(rel_path, deny_patterns)
        for motif in path_motifs:
            findings.append(f"chemin#{index}: {motif}")

        file_path = repo / rel_path
        if not file_path.is_file():
            continue
        try:
            raw_bytes = file_path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw_bytes:
            continue
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue

        display = rel_path if not path_motifs else f"fichier#{index}"
        for lineno, line in enumerate(text.splitlines(), start=1):
            for motif in _check_text_line(line, deny_patterns):
                findings.append(f"{display}:{lineno}: {motif}")

    return findings


def _check_commit_range(
    repo: Path,
    commit_range: str,
    deny_patterns: list[tuple[str, re.Pattern[str]]],
) -> list[str]:
    findings: list[str] = []
    if ".." not in commit_range:
        return findings
    start_sha, end_sha = commit_range.split("..", 1)
    if end_sha == ZERO_SHA:
        return findings

    if start_sha == ZERO_SHA:
        rev_cmd = ["git", "-C", str(repo), "rev-list", end_sha, "--not", "--remotes"]
    else:
        rev_cmd = ["git", "-C", str(repo), "rev-list", f"{start_sha}..{end_sha}"]

    rev_proc = subprocess.run(rev_cmd, capture_output=True, text=True, check=False)
    if rev_proc.returncode != 0:
        return findings

    shas = [s.strip() for s in rev_proc.stdout.splitlines() if s.strip()]
    for sha in shas:
        show_proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "show",
                "-s",
                "--format=%h%x00%an%x00%ae%x00%cn%x00%ce%x00%B",
                sha,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if show_proc.returncode != 0:
            continue
        parts = show_proc.stdout.split("\x00", 5)
        if len(parts) < 6:
            continue
        short_sha, author_name, author_email, committer_name, committer_email, body = parts

        fields = [
            ("author", author_name),
            ("author-email", author_email),
            ("committer", committer_name),
            ("committer-email", committer_email),
            ("message", body),
        ]
        for field_name, value in fields:
            motifs: list[str] = []
            for line in value.splitlines() or [value]:
                motifs.extend(_check_text_line(line, deny_patterns))
            if field_name in ("author-email", "committer-email") and "@" in value:
                domain = value.rsplit("@", 1)[-1].strip().lower()
                if (
                    value.strip().lower() not in ALLOWED_EMAIL_ADDRESSES
                    and domain not in ALLOWED_EMAIL_DOMAINS
                    and "email-non-autorise" not in motifs
                ):
                    motifs.append("email-non-autorise")
            for motif in motifs:
                findings.append(f"commit {short_sha}: {field_name}: {motif}")

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Contrôle de confidentialité des fichiers suivis et des commits Git."
    )
    parser.add_argument("--repo", default=".", help="Répertoire du dépôt Git à analyser.")
    parser.add_argument("--deny-file", default=None, help="Chemin de la liste privée de motifs.")
    parser.add_argument(
        "--generic",
        action="store_true",
        help="Exécute uniquement les contrôles génériques sans lire la liste privée.",
    )
    parser.add_argument("--range", dest="commit_range", default=None, help="Plage de commits A..B.")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    deny_patterns: list[tuple[str, re.Pattern[str]]] = []
    if not args.generic:
        deny_path = Path(args.deny_file).expanduser() if args.deny_file else _default_deny_file()
        deny_patterns = _load_deny_patterns(deny_path)

    findings = _check_tracked_files(repo, deny_patterns)
    if args.commit_range:
        findings.extend(_check_commit_range(repo, args.commit_range, deny_patterns))

    for item in findings:
        print(item)

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
