"""Reader for No Man's Sky `.pak` archives (HGPAK v2 + Zstandard).

Format per planet-data.md 0.1, re-verified here against the shipped archives:

    0x00  8     "HGPAK\\0\\0\\0"
    0x08  8     version (2)
    0x10  8     file count N
    0x18  8     chunk count C
    0x20  8     unknown, always 1
    0x28  8     data offset D
    0x30  N*32  entries { u8 name_hash[16]; u64 virtual_offset; u64 size }
          C*8   chunk table: compressed size per chunk, in order
    D     ...   payloads: raw Zstd frames, each decompressing to exactly 0x10000
                bytes (the last may be short), padded to a 16-byte boundary

`virtual_offset` indexes the concatenation of every decompressed chunk, biased
by D. File entry 0 is a plaintext CRLF manifest naming entries 1..N-1 in order,
so the 16-byte hashes never have to be inverted.

Only the chunks a wanted file actually spans are decompressed, which is what
makes pulling one table out of a 14,755-file archive cheap.
"""
import os
import struct
import sys

try:
    import zstandard
except ImportError:
    sys.exit("needs the zstandard package:  python -m pip install zstandard")

CHUNK = 0x10000
MAGIC = b"HGPAK\0\0\0"


class Pak(object):
    def __init__(self, path):
        self.path = path
        self.fh = open(path, "rb")
        head = self.fh.read(0x30)
        if head[:8] != MAGIC:
            raise ValueError("%s is not an HGPAK archive" % path)
        (self.version, self.count, self.chunks,
         _unknown, self.data_off) = struct.unpack_from("<QQQQQ", head, 8)

        self.fh.seek(0x30)
        self.entries = []
        for _ in range(self.count):
            blob = self.fh.read(32)
            off, size = struct.unpack_from("<QQ", blob, 16)
            self.entries.append((off, size))

        self.csizes = list(struct.unpack("<%dQ" % self.chunks,
                                         self.fh.read(8 * self.chunks)))
        # Where each chunk starts on disk. Every frame is padded to 16 bytes.
        self.coffs, at = [], self.data_off
        for c in self.csizes:
            self.coffs.append(at)
            at += (c + 15) & ~15

        self._dctx = zstandard.ZstdDecompressor()
        self._cache = {}
        self.names = self._manifest()

    def _chunk(self, i):
        if i not in self._cache:
            self.fh.seek(self.coffs[i])
            raw = self.fh.read(self.csizes[i])
            self._cache[i] = self._dctx.decompress(raw, max_output_size=CHUNK)
            if len(self._cache) > 64:          # bounded: these are 64 KB each
                self._cache.pop(next(iter(self._cache)))
        return self._cache[i]

    def read_entry(self, index):
        off, size = self.entries[index]
        start = off - self.data_off
        out = bytearray()
        first, last = start // CHUNK, (start + size - 1) // CHUNK
        for c in range(first, last + 1):
            out += self._chunk(c)
        head = start - first * CHUNK
        return bytes(out[head:head + size])

    def _manifest(self):
        text = self.read_entry(0).decode("utf-8", "surrogateescape")
        names = [n.strip() for n in text.replace("\r\n", "\n").split("\n") if n.strip()]
        return names

    def find(self, needle):
        """-> [(index, name)] for every manifest name containing `needle`."""
        n = needle.upper()
        return [(i + 1, nm) for i, nm in enumerate(self.names) if n in nm.upper()]

    def extract(self, index, dest):
        data = self.read_entry(index)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
        return len(data)

    def close(self):
        self.fh.close()


def main(argv):
    if len(argv) < 3:
        print("usage: hgpak.py <pak-or-dir> <substring> [outdir]")
        return 2
    src, needle = argv[1], argv[2]
    outdir = argv[3] if len(argv) > 3 else None
    paks = ([os.path.join(src, f) for f in sorted(os.listdir(src)) if f.lower().endswith(".pak")]
            if os.path.isdir(src) else [src])
    for p in paks:
        try:
            pak = Pak(p)
        except Exception as exc:
            print("%-42s skipped (%s)" % (os.path.basename(p), exc))
            continue
        hits = pak.find(needle)
        if hits:
            print("%-42s %d file(s), %d in manifest" % (os.path.basename(p), len(hits), len(pak.names)))
            for idx, nm in hits:
                print("   [%6d] %s" % (idx, nm))
                if outdir:
                    dest = os.path.join(outdir, nm.replace("\\", "/").lstrip("/"))
                    print("            -> %s (%d bytes)" % (dest, pak.extract(idx, dest)))
        pak.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
