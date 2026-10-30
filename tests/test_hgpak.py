"""`tools/hgpak.py` reads a chunk that is stored, not framed.

HGPAK chunks are Zstd frames of exactly 0x10000 bytes, except when the frame
would not be smaller than the bytes it carries: then the chunk is stored
verbatim at its own length. The texture archives are full of those, and a
reader that assumed every chunk had a frame header failed on the first one with
"unknown frame descriptor" and aborted the whole pull, which is how a
regeneration died halfway through `docs/POST-PATCH.md` step 1.

The fixture here is a two-chunk archive in the same layout: the first chunk
stored, the second framed, with the manifest and two entries laid across them.
"""
import struct

import zstandard

from tools import hgpak


def _pak(tmp_path):
    """-> path, with a stored chunk 0 and a framed chunk 1."""
    manifest = "a.txt\r\nb.bin\r\n"
    entry_a = b"hello stored"
    entry_b = b"x" * 100
    assert len(manifest) + len(entry_a) <= hgpak.CHUNK
    stored = (manifest.encode() + entry_a).ljust(hgpak.CHUNK, b"\0")
    framed = entry_b.ljust(hgpak.CHUNK, b"\0")

    payload_1 = zstandard.ZstdCompressor().compress(framed)
    chunks = [stored, payload_1]

    count = 3
    data_off = 0x30 + 32 * count + 8 * len(chunks)
    entries = [(data_off, len(manifest)),
               (data_off + len(manifest), len(entry_a)),
               (data_off + hgpak.CHUNK, len(entry_b))]
    head = hgpak.MAGIC + struct.pack("<QQQQQ", 2, count, len(chunks), 1,
                                     data_off)
    body = b"".join(b"\0" * 16 + struct.pack("<QQ", off, size)
                    for off, size in entries)
    body += b"".join(struct.pack("<Q", len(c)) for c in chunks)
    for c in chunks:
        body += c.ljust((len(c) + 15) & ~15, b"\0")

    path = tmp_path / "test.pak"
    path.write_bytes(head + body)
    return path


def test_a_stored_chunk_reads_back_verbatim(tmp_path):
    """The manifest lives in the stored chunk, so the archive only opens at all
    if a verbatim chunk is not handed to the decompressor."""
    pak = hgpak.Pak(str(_pak(tmp_path)))
    try:
        assert pak.names == ["a.txt", "b.bin"]
        assert pak.read_entry(1) == b"hello stored"
    finally:
        pak.close()


def test_a_framed_chunk_still_decompresses(tmp_path):
    pak = hgpak.Pak(str(_pak(tmp_path)))
    try:
        assert pak.read_entry(2) == b"x" * 100
        assert pak.find("b.bin") == [(2, "b.bin")]
    finally:
        pak.close()
