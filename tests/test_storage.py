import boto3
import pytest
from moto import mock_aws

from second_brain.storage import LocalStorage, S3Storage, StorageError, clean_path


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/../../b.md", ".obsidian/app.json", ".git/config", "", "a\x00.md"])
def test_clean_path_rejects_escapes_and_hidden(bad):
    with pytest.raises(StorageError):
        clean_path(bad)


def test_clean_path_normalizes():
    assert clean_path("/People/Dana Rivers.md") == "People/Dana Rivers.md"
    assert clean_path("People\\Dana.md") == "People/Dana.md"


def test_local_storage_cannot_escape_root(tmp_path):
    (tmp_path / "v").mkdir()
    (tmp_path / "secret.md").write_text("nope")
    s = LocalStorage(tmp_path / "v")
    with pytest.raises(StorageError):
        s.read("../secret.md")


def test_local_storage_skips_hidden_dirs(tmp_path):
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "x.md").write_text("x")
    (tmp_path / "a.md").write_text("a")
    assert LocalStorage(tmp_path).list() == ["a.md"]


@mock_aws
def test_s3_roundtrip_move_and_prefix():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="brain")
    s = S3Storage("brain", prefix="vault", client=client)
    s.write("People/Dana.md", "hello")
    assert s.exists("People/Dana.md")
    assert s.read("People/Dana.md") == "hello"
    assert s.list() == ["People/Dana.md"]
    s.move("People/Dana.md", "Archive/People/Dana.md")
    assert not s.exists("People/Dana.md")
    assert s.list() == ["Archive/People/Dana.md"]
    with pytest.raises(StorageError):
        s.read("People/Dana.md")
    keys = [o["Key"] for o in client.list_objects_v2(Bucket="brain")["Contents"]]
    assert keys == ["vault/Archive/People/Dana.md"]
