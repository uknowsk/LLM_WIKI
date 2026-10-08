"""vault_backup: zip snapshots of the second-brain folders into a backup folder, skip unchanged, keep N, restore safely."""
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from llmwiki.vault_backup import BackupError, backup, main, restore

T0 = datetime(2026, 10, 9, 21, 0, 0)


def make_vault(root: Path) -> Path:
    files = {"10_raw_data/2026-09-15_회의.md": "원본 내용 3,500만원".encode(), "10_raw_data/_files/scan.pdf": b"%PDF-1.4 \x00\xff",
             "10_raw_data/.manifest.json": b"{}", "20_ai_wiki/알파.md": "위키".encode(), "30_outputs/2026-10-08_뉴스.md": "산출물".encode(),
             "src/other.py": b"not part of the vault", "README.md": b"not part of the vault"}
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def names(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as z:
        return set(z.namelist())


def test_snapshot_contains_exactly_the_three_folders_with_every_file_type(tmp_path):
    v, dest = make_vault(tmp_path / "v"), tmp_path / "bk"
    r = backup(v, dest, now=T0)
    assert r.status == "created" and r.path.parent == dest and r.path.name == "second-brain-20261009-210000.zip"
    assert names(r.path) == {"10_raw_data/2026-09-15_회의.md", "10_raw_data/_files/scan.pdf", "10_raw_data/.manifest.json",
                             "20_ai_wiki/알파.md", "30_outputs/2026-10-08_뉴스.md"}
    assert r.files == 5


def test_unchanged_vault_makes_no_new_snapshot_but_a_change_does(tmp_path):
    v, dest = make_vault(tmp_path / "v"), tmp_path / "bk"
    backup(v, dest, now=T0)
    again = backup(v, dest, now=T0 + timedelta(days=1))
    assert again.status == "unchanged" and len(list(dest.glob("*.zip"))) == 1
    (v / "20_ai_wiki/알파.md").write_bytes("위키 수정".encode())
    assert backup(v, dest, now=T0 + timedelta(days=2)).status == "created" and len(list(dest.glob("*.zip"))) == 2


def test_only_the_newest_snapshots_are_kept(tmp_path):
    v, dest = make_vault(tmp_path / "v"), tmp_path / "bk"
    for i in range(4):
        (v / "20_ai_wiki/알파.md").write_bytes(f"v{i}".encode())
        r = backup(v, dest, keep=2, now=T0 + timedelta(days=i))
    left = sorted(p.name for p in dest.glob("*.zip"))
    assert left == ["second-brain-20261011-210000.zip", "second-brain-20261012-210000.zip"] and r.pruned == 1


def test_restore_round_trip_and_never_overwrites(tmp_path):
    v, dest = make_vault(tmp_path / "v"), tmp_path / "bk"
    snap = backup(v, dest, now=T0).path
    out = tmp_path / "restored"
    assert restore(snap, out) == 5
    for rel in ("10_raw_data/2026-09-15_회의.md", "10_raw_data/_files/scan.pdf", "20_ai_wiki/알파.md"):
        assert (out / rel).read_bytes() == (v / rel).read_bytes()
    with pytest.raises(BackupError):  # a non-empty target is refused: a restore must never replace existing notes
        restore(snap, out)


def test_restore_rejects_paths_that_escape_the_target(tmp_path):
    bad = tmp_path / "evil.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../escaped.txt", "x")
    with pytest.raises(BackupError):
        restore(bad, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()


def test_destination_inside_a_vault_folder_is_refused(tmp_path, capsys):
    v = make_vault(tmp_path / "v")
    assert main(["backup", "--vault", str(v), "--dest", str(v / "20_ai_wiki" / "bk")]) == 2
    assert "inside" in capsys.readouterr().err
    assert not (v / "20_ai_wiki" / "bk").exists()


def test_missing_vault_folders_are_a_clear_error(tmp_path, capsys):
    assert main(["backup", "--vault", str(tmp_path), "--dest", str(tmp_path / "bk")]) == 2
    assert "10_raw_data" in capsys.readouterr().err


def test_cli_uses_the_env_default_destination(tmp_path, monkeypatch, capsys):
    v = make_vault(tmp_path / "v")
    monkeypatch.setenv("WIKI_VAULT_BACKUP_DIR", str(tmp_path / "from-env"))
    assert main(["backup", "--vault", str(v)]) == 0
    assert len(list((tmp_path / "from-env").glob("second-brain-*.zip"))) == 1
    assert "created" in capsys.readouterr().out
