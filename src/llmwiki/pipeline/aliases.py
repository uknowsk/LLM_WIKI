"""Folder aliases for the shared inbox (WIKI_INBOX_ALIASES): readable folder names -> canonical space.

    {"인사팀": "dept-hr", "인프라팀/당직": "dept-a/part-1"}

The alias file only renames the folder -> space mapping. WINDOWS (NTFS/share ACLs) still decides who may write
into a folder; the space is derived from the folder alone, never from file content. Any problem in the file is a
startup refusal (fail closed): a half-applied mapping could route files into the wrong department's space.

Windows folders are case-insensitive and Korean names may arrive as NFC (Windows) or NFD (macOS, some clients),
so keys and folder names are compared after NFC normalisation + casefold. Duplicate detection uses the same form.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from llmwiki.ingest.save import validate_space as _canonical
from llmwiki.pipeline.inbox import InvalidSpace, validate_space

MAX_ALIAS_BYTES = 64 * 1024
MAX_SEGMENTS = 4
_BAD_CHARS = re.compile(r'[\\:*?"<>|\x00-\x1f]')
_DEVICES = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})


class AliasError(RuntimeError):
    """Invalid alias file: the pipeline must refuse to start."""


def norm(part: str) -> str:
    """Comparison form of one folder name (NFC + casefold; Windows treats case as insignificant)."""
    return unicodedata.normalize("NFC", part).casefold()


@dataclass(frozen=True)
class AliasMap:
    entries: dict[str, str]  # NFC key -> canonical space (for display / doctor)
    _index: dict[tuple[str, ...], str]

    def resolve(self, folder_parts: Sequence[str]) -> str | None:
        """Longest alias prefix of the folder path wins; sub-folders below an alias inherit its space."""
        if not 0 < len(folder_parts) <= MAX_SEGMENTS:
            return None
        normed = tuple(norm(p) for p in folder_parts)
        for n in range(len(normed), 0, -1):
            hit = self._index.get(normed[:n])
            if hit is not None:
                return hit
        return None


def _check_key(key: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(key, str) or not key:
        raise AliasError("별칭 키는 비어 있지 않은 문자열이어야 합니다")
    nfc = unicodedata.normalize("NFC", key)
    if nfc != nfc.strip():
        raise AliasError(f"별칭 키 앞뒤에 공백이 있습니다: {key!r}")
    if "\\" in nfc or ":" in nfc:
        raise AliasError(f"별칭 키에 역슬래시/드라이브 문자(\\ :)를 쓸 수 없습니다: {key!r}")
    segs = nfc.split("/")
    if len(segs) > MAX_SEGMENTS:
        raise AliasError(f"별칭 키는 최대 {MAX_SEGMENTS}단계입니다: {key!r}")
    for seg in segs:
        if not seg or seg in (".", "..") or ".." in seg:
            raise AliasError(f"별칭 키에 빈 단계/'.'/'..' 를 쓸 수 없습니다: {key!r}")
        if _BAD_CHARS.search(seg):
            raise AliasError(f"별칭 키에 Windows 폴더에 쓸 수 없는 문자가 있습니다: {key!r}")
        if seg.startswith("_"):
            raise AliasError(f"'_' 로 시작하는 폴더 이름은 예약되어 있습니다: {key!r}")
        if seg.endswith((".", " ")):
            raise AliasError(f"폴더 이름이 점/공백으로 끝날 수 없습니다 (Windows): {key!r}")
        if seg.split(".")[0].casefold() in _DEVICES:
            raise AliasError(f"Windows 예약 이름은 쓸 수 없습니다: {key!r}")
    return nfc, tuple(norm(s) for s in segs)


def _canonical_prefix(normed: tuple[str, ...]) -> str | None:
    """Longest prefix of the key that is itself a valid canonical space name (as Windows would see the folder)."""
    for n in range(len(normed), 0, -1):
        cand = "/".join(normed[:n])
        try:
            _canonical(cand)
            return cand
        except ValueError:
            continue
    return None


def _no_dupes(pairs: list[tuple[str, object]]) -> dict:
    d: dict = {}
    for k, v in pairs:
        if k in d:
            raise AliasError(f"중복된 별칭 키: {k!r}")
        d[k] = v
    return d


def parse_aliases(raw: bytes) -> AliasMap:
    if len(raw) > MAX_ALIAS_BYTES:
        raise AliasError(f"별칭 파일이 너무 큽니다 (최대 {MAX_ALIAS_BYTES // 1024} KiB)")
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_no_dupes)
    except AliasError:
        raise
    except (ValueError, RecursionError):
        raise AliasError("별칭 파일이 UTF-8 JSON 이 아닙니다") from None
    if not isinstance(data, dict):
        raise AliasError('별칭 파일은 JSON 객체여야 합니다: {"인사팀": "dept-hr"}')
    entries: dict[str, str] = {}
    index: dict[tuple[str, ...], str] = {}
    for key, space in data.items():
        nfc, normed = _check_key(key)
        if normed in index:  # same folder as far as Windows is concerned (case / NFC vs NFD)
            raise AliasError(f"대소문자/유니코드 정규화만 다른 중복 별칭 키: {key!r}")
        if not isinstance(space, str):
            raise AliasError(f"별칭 값은 space 이름 문자열이어야 합니다: {key!r}")
        try:
            validate_space(space)
        except InvalidSpace:
            raise AliasError(f"별칭 {key!r} 의 값이 올바른 space 이름이 아닙니다") from None
        canon = _canonical_prefix(normed)
        if canon is not None and not (space == canon or space.startswith(canon + "/")):
            # a folder that Windows/the pipeline already reads as department `canon` must not be redirected elsewhere
            raise AliasError(f"별칭 {key!r} 는 다른 부서의 정식 space 폴더({canon})와 충돌합니다")
        entries[nfc] = space
        index[normed] = space
    for normed, space in index.items():  # a nested folder must stay inside its parent alias's department
        for n in range(1, len(normed)):
            parent = index.get(normed[:n])
            if parent is not None and not (space == parent or space.startswith(parent + "/")):
                raise AliasError(f"별칭 {'/'.join(normed)!r} 가 상위 별칭({'/'.join(normed[:n])!r} -> {parent})과 다른 부서를 가리킵니다")
    return AliasMap(entries, index)


def load_aliases(environ: Mapping[str, str] | None = None) -> AliasMap | None:
    """None when WIKI_INBOX_ALIASES is unset. Any read/parse/validation problem raises AliasError."""
    e = os.environ if environ is None else environ
    raw_path = (e.get("WIKI_INBOX_ALIASES") or "").strip()
    if not raw_path:
        return None
    try:
        p = Path(raw_path)
        if p.stat().st_size > MAX_ALIAS_BYTES:
            raise AliasError(f"별칭 파일이 너무 큽니다 (최대 {MAX_ALIAS_BYTES // 1024} KiB)")
        raw = p.read_bytes()
    except AliasError:
        raise
    except OSError as exc:
        raise AliasError(f"WIKI_INBOX_ALIASES 파일을 읽을 수 없습니다 ({type(exc).__name__})") from None
    return parse_aliases(raw)
