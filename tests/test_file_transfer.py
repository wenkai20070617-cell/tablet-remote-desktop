import asyncio
import contextlib
import hashlib
import shutil
import unittest
import uuid
from pathlib import Path

from app.file_transfer import (
    CHUNK_READ,
    FileTransferError,
    TransferManager,
    UnsafePath,
    directory_listing,
    safe_name,
    safe_relative,
    unique_path,
)

# Temporary trees live beside the tests: the system temp directory is not
# always writable and cleanable in every environment.
SCRATCH = Path(__file__).resolve().parent / "_scratch"


@contextlib.contextmanager
def _cleanup(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def scratch_dir() -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    return SCRATCH / uuid.uuid4().hex


class SafePathTests(unittest.TestCase):
    def test_plain_names_are_kept(self):
        self.assertEqual(safe_name("report.pdf"), "report.pdf")

    def test_directories_are_stripped_from_a_name(self):
        self.assertEqual(safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(safe_name("C:\\Users\\me\\secret.txt"), "secret.txt")

    def test_dangerous_characters_are_removed(self):
        self.assertEqual(safe_name('a<b>c:d"e|f?g*h.txt'), "abcdefgh.txt")

    def test_empty_and_dot_names_are_rejected(self):
        for value in ("", "   ", ".", "..", "/"):
            with self.assertRaises(UnsafePath):
                safe_name(value)

    def test_relative_paths_reject_traversal_and_absolute_paths(self):
        for value in ("../secret", "a/../../b", "/etc/passwd", "C:/Windows/system32"):
            with self.assertRaises(UnsafePath):
                safe_relative(value)

    def test_relative_paths_are_normalised(self):
        self.assertEqual(str(safe_relative("docs/2024/report.pdf")), str(Path("docs/2024/report.pdf")))
        self.assertEqual(str(safe_relative("./docs//report.pdf")), str(Path("docs/report.pdf")))

    def test_deeply_nested_paths_are_rejected(self):
        with self.assertRaises(UnsafePath):
            safe_relative("/".join(f"d{index}" for index in range(20)))

    def test_unique_path_avoids_overwriting(self):
        with _cleanup(scratch_dir()) as temporary:
            root = Path(temporary)
            (root / "file.txt").write_text("first", encoding="utf-8")
            chosen = unique_path(root, "file.txt")
            self.assertNotEqual(chosen.name, "file.txt")
            self.assertTrue(chosen.name.startswith("file"))


class DirectoryListingTests(unittest.TestCase):
    def test_listing_reports_entries_and_rejects_escape(self):
        with _cleanup(scratch_dir()) as temporary:
            root = Path(temporary)
            (root / "sub").mkdir()
            (root / "a.txt").write_text("data", encoding="utf-8")
            listing = directory_listing(root, "")
            names = [entry["name"] for entry in listing["entries"]]
            self.assertIn("a.txt", names)
            self.assertIn("sub", names)
            self.assertEqual(listing["path"], "")
            with self.assertRaises(UnsafePath):
                directory_listing(root, "../")
            with self.assertRaises(FileTransferError):
                directory_listing(root, "does-not-exist")


class TransferManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temporary = _cleanup(scratch_dir())
        root = Path(self._temporary.__enter__())
        self.upload = root / "upload"
        self.download = root / "download"
        self.manager = TransferManager(
            upload_root=self.upload,
            download_root=self.download,
            max_size=5_000_000,
            reserve_free_bytes=0,
        )
        self.payload = bytes(range(256)) * 200

    def tearDown(self):
        self._temporary.__exit__(None, None, None)

    async def asyncTearDown(self):
        await self.manager.close()

    async def test_full_upload_verifies_the_checksum(self):
        transfer = await self.manager.begin("data.bin", len(self.payload))
        await self.manager.write_chunk(transfer.transfer_id, 0, self.payload)
        result = await self.manager.finish(transfer.transfer_id)
        self.assertEqual(result["sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(Path(result["path"]).read_bytes(), self.payload)

    async def test_a_replayed_chunk_reports_the_resume_offset(self):
        half = len(self.payload) // 2
        transfer = await self.manager.begin("resume.bin", len(self.payload))
        first = await self.manager.write_chunk(transfer.transfer_id, 0, self.payload[:half])
        self.assertTrue(first["ok"])
        replay = await self.manager.write_chunk(transfer.transfer_id, 0, self.payload[:half])
        self.assertFalse(replay["ok"])
        self.assertEqual(replay["expectedOffset"], half)

    async def test_an_interrupted_upload_resumes(self):
        half = len(self.payload) // 2
        transfer = await self.manager.begin("resume.bin", len(self.payload))
        await self.manager.write_chunk(transfer.transfer_id, 0, self.payload[:half])
        status = await self.manager.status("resume.bin", len(self.payload))
        self.assertTrue(status["known"])
        self.assertEqual(status["received"], half)
        # A resumed session must continue from the stored offset, not from zero.
        again = await self.manager.begin("resume.bin", len(self.payload))
        self.assertEqual(again.received, half)
        await self.manager.write_chunk(again.transfer_id, half, self.payload[half:])
        result = await self.manager.finish(again.transfer_id)
        self.assertEqual(Path(result["path"]).read_bytes(), self.payload)

    async def test_finishing_early_is_refused(self):
        transfer = await self.manager.begin("short.bin", len(self.payload))
        await self.manager.write_chunk(transfer.transfer_id, 0, self.payload[:10])
        with self.assertRaises(FileTransferError):
            await self.manager.finish(transfer.transfer_id)

    async def test_a_checksum_mismatch_discards_the_upload(self):
        transfer = await self.manager.begin("bad.bin", len(self.payload))
        await self.manager.write_chunk(transfer.transfer_id, 0, self.payload)
        with self.assertRaises(FileTransferError):
            await self.manager.finish(transfer.transfer_id, "00" * 32)
        self.assertFalse(list(self.upload.glob("bad.bin")))
        self.assertFalse(list(self.upload.glob("*.part")))

    async def test_each_chunk_can_be_verified_before_it_is_written(self):
        transfer = await self.manager.begin("verified.bin", len(self.payload))
        digest = hashlib.sha256(self.payload).hexdigest()
        await self.manager.write_chunk(transfer.transfer_id, 0, self.payload, digest)
        result = await self.manager.finish(transfer.transfer_id, digest)
        self.assertEqual(result["sha256"], digest)

    async def test_a_bad_chunk_checksum_is_rejected_without_advancing(self):
        transfer = await self.manager.begin("bad-chunk.bin", len(self.payload))
        with self.assertRaises(FileTransferError):
            await self.manager.write_chunk(transfer.transfer_id, 0, self.payload, "00" * 32)
        self.assertEqual(transfer.received, 0)

    async def test_a_complete_partial_is_rehashed_before_finish(self):
        transfer = await self.manager.begin("complete-part.bin", len(self.payload))
        await self.manager.write_chunk(transfer.transfer_id, 0, self.payload)
        await self.manager.cancel(transfer.transfer_id, remove_partial=False)
        resumed = TransferManager(
            upload_root=self.upload,
            download_root=self.download,
            max_size=5_000_000,
            reserve_free_bytes=0,
        )
        recovered = await resumed.begin("complete-part.bin", len(self.payload))
        self.assertEqual(recovered.received, len(self.payload))
        expected_blocks = [
            hashlib.sha256(self.payload[index:index + CHUNK_READ]).hexdigest()
            for index in range(0, len(self.payload), CHUNK_READ)
        ]
        self.assertEqual(recovered.resume_hashes, expected_blocks)
        result = await resumed.finish(recovered.transfer_id, hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(result["sha256"], hashlib.sha256(self.payload).hexdigest())

    async def test_an_oversized_file_is_refused(self):
        with self.assertRaises(FileTransferError):
            await self.manager.begin("big.bin", 50_000_000)

    async def test_a_chunk_beyond_the_declared_size_is_refused(self):
        transfer = await self.manager.begin("small.bin", 10)
        with self.assertRaises(FileTransferError):
            await self.manager.write_chunk(transfer.transfer_id, 0, b"x" * 20)

    async def test_cancelling_keeps_or_removes_the_partial_file(self):
        transfer = await self.manager.begin("cancel.bin", len(self.payload))
        await self.manager.write_chunk(transfer.transfer_id, 0, self.payload[:100])
        result = await self.manager.cancel(transfer.transfer_id, remove_partial=True)
        self.assertTrue(result["known"])
        self.assertEqual([], list(self.upload.glob(".*part")))
        with self.assertRaises(FileTransferError):
            await self.manager.get(transfer.transfer_id)

    async def test_writing_to_an_unknown_transfer_is_refused(self):
        with self.assertRaises(FileTransferError):
            await self.manager.write_chunk("does-not-exist", 0, b"data")

    async def test_subdirectories_are_honoured(self):
        transfer = await self.manager.begin("nested.bin", 4, "docs/2024/nested.bin")
        await self.manager.write_chunk(transfer.transfer_id, 0, b"data")
        result = await self.manager.finish(transfer.transfer_id)
        self.assertTrue(str(result["path"]).endswith(str(Path("docs") / "2024" / "nested.bin")))

    async def test_an_existing_file_is_not_overwritten_by_default(self):
        (self.upload / "keep.bin").write_bytes(b"original")
        transfer = await self.manager.begin("keep.bin", 4)
        await self.manager.write_chunk(transfer.transfer_id, 0, b"data")
        result = await self.manager.finish(transfer.transfer_id)
        self.assertEqual((self.upload / "keep.bin").read_bytes(), b"original")
        self.assertNotEqual(Path(result["path"]).name, "keep.bin")

    async def test_download_resolution_rejects_escapes(self):
        (self.download / "ok.txt").write_text("ok", encoding="utf-8")
        self.assertEqual(self.manager.resolve_download("ok.txt").name, "ok.txt")
        for value in ("../outside.txt", "/etc/passwd", "a/../../b"):
            with self.assertRaises(UnsafePath):
                self.manager.resolve_download(value)
        with self.assertRaises(FileTransferError):
            self.manager.resolve_download("missing.txt")

    async def test_delete_removes_only_a_regular_file_below_the_selected_root(self):
        target = self.upload / "remove.txt"
        target.write_text("remove", encoding="utf-8")
        result = self.manager.delete_file("remove.txt", "upload")
        self.assertTrue(result["ok"])
        self.assertFalse(target.exists())
        (self.upload / "folder").mkdir()
        with self.assertRaises(FileTransferError):
            self.manager.delete_file("folder", "upload")
        with self.assertRaises(UnsafePath):
            self.manager.delete_file("../outside.txt", "upload")


if __name__ == "__main__":
    unittest.main()
