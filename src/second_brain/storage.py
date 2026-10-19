"""Storage backends for vault files. Paths are always vault-relative POSIX paths."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Protocol


class StorageError(Exception):
    pass


def clean_path(path: str) -> str:
    """Normalize a vault-relative path and reject anything that escapes the vault."""
    if "\x00" in path:
        raise StorageError("Invalid path")
    p = PurePosixPath(path.replace("\\", "/").lstrip("/"))
    if any(part in ("..", "") for part in p.parts) or not p.parts:
        raise StorageError(f"Invalid path: {path!r}")
    # Reject hidden segments at any depth, not just the top level: a nested hidden folder
    # (e.g. Resources/.exfil/x.md) would otherwise be written yet stay invisible to list(),
    # search and health, which all skip dot-segments.
    if any(part.startswith(".") for part in p.parts):
        raise StorageError(f"Hidden paths are not accessible: {path!r}")
    return p.as_posix()


class Storage(Protocol):
    def list(self) -> list[str]: ...
    def read(self, path: str) -> str: ...
    def write(self, path: str, content: str) -> None: ...
    def move(self, src: str, dst: str) -> None: ...
    def exists(self, path: str) -> bool: ...


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise StorageError(f"Vault directory not found: {self.root}")

    def _abs(self, path: str) -> Path:
        p = (self.root / clean_path(path)).resolve()
        if not p.is_relative_to(self.root):
            raise StorageError(f"Path escapes vault: {path!r}")
        return p

    def list(self) -> list[str]:
        out = []
        for p in self.root.rglob("*.md"):
            rel = p.relative_to(self.root)
            if not any(part.startswith(".") for part in rel.parts):
                out.append(rel.as_posix())
        return sorted(out)

    def read(self, path: str) -> str:
        p = self._abs(path)
        if not p.is_file():
            raise StorageError(f"Note not found: {path}")
        return p.read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> None:
        p = self._abs(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(p)

    def move(self, src: str, dst: str) -> None:
        s, d = self._abs(src), self._abs(dst)
        if d.exists():
            raise StorageError(f"Destination exists: {dst}")
        d.parent.mkdir(parents=True, exist_ok=True)
        s.rename(d)

    def exists(self, path: str) -> bool:
        return self._abs(path).is_file()


class S3Storage:
    """S3 backend. Relies on bucket versioning for history; never issues hard deletes
    except for the source object of a move (its prior versions are retained)."""

    def __init__(
        self, bucket: str, prefix: str = "", region: str | None = None, kms_key_id: str | None = None, client=None
    ):
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.kms_key_id = kms_key_id
        self.s3 = client or boto3.client("s3", region_name=region)

    def _key(self, path: str) -> str:
        return self.prefix + clean_path(path)

    def list(self) -> list[str]:
        out = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(self.prefix) :]
                if rel.endswith(".md") and not any(p.startswith(".") for p in rel.split("/")):
                    out.append(rel)
        return sorted(out)

    def read(self, path: str) -> str:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self._key(path))
        except self.s3.exceptions.NoSuchKey as e:
            raise StorageError(f"Note not found: {path}") from e
        return obj["Body"].read().decode("utf-8")

    def write(self, path: str, content: str) -> None:
        extra = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.kms_key_id} if self.kms_key_id else {}
        self.s3.put_object(
            Bucket=self.bucket,
            Key=self._key(path),
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
            **extra,
        )

    def move(self, src: str, dst: str) -> None:
        if self.exists(dst):
            raise StorageError(f"Destination exists: {dst}")
        self.write(dst, self.read(src))
        self.s3.delete_object(Bucket=self.bucket, Key=self._key(src))

    def exists(self, path: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key(path))
            return True
        except self.s3.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
