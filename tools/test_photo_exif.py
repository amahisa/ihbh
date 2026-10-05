"""photo_exif.py のテスト（実行: python3 -m unittest discover -s tools）"""
import json
import os
import struct
import tempfile
import unittest

import photo_exif as pe


# ---------- テスト用にバイト列を組み立てる補助 ----------
def _encode(e, typ, val, ptrs):
    """EXIFの値を (count, bytes) にする。typ: 2=ASCII 4=LONG 5=RATIONAL"""
    if typ == 2:
        b = val.encode() + b"\0"
        return len(b), b
    if typ == 5:
        return len(val), b"".join(struct.pack(e + "II", n, d) for n, d in val)
    if typ == 4:
        return 1, struct.pack(e + "I", ptrs[val] if isinstance(val, str) else val)
    raise ValueError(typ)


def build_tiff(e, ifds):
    """ifds: {名前: [(tag, type, value), ...]}（先頭が IFD0）。type=4 の value に IFD 名を渡すとその位置を指す"""
    ptrs, pos = {}, 8
    for name, entries in ifds.items():
        ptrs[name] = pos
        pos += 2 + 12 * len(entries) + 4
    body, data = b"", b""
    for name, entries in ifds.items():
        body += struct.pack(e + "H", len(entries))
        for tag, typ, val in sorted(entries):
            count, raw = _encode(e, typ, val, ptrs)
            if len(raw) <= 4:
                field = raw.ljust(4, b"\0")
            else:
                field = struct.pack(e + "I", pos + len(data))
                data += raw + (b"\0" if len(raw) % 2 else b"")
            body += struct.pack(e + "HHI", tag, typ, count) + field
        body += b"\0\0\0\0"
    return (b"II" if e == "<" else b"MM") + struct.pack(e + "HI", 42, 8) + body + data


def rationals(v, scale=1000000):
    """小数の度を 度・分・秒 の有理数3つにする"""
    d = int(v)
    m = int((v - d) * 60)
    s = round(((v - d) * 60 - m) * 60 * scale)
    return [(d, 1), (m, 1), (s, scale)]


def exif_tiff(e="<", lat=35.681236, lon=139.767125, dt="2024:05:03 14:23:11", offset="+09:00"):
    ifds = {"ifd0": [], "exif": [], "gps": []}
    if dt:
        ifds["exif"].append((0x9003, 2, dt))
        ifds["ifd0"].append((0x8769, 4, "exif"))
    if offset:
        ifds["exif"].append((0x9011, 2, offset))
    if lat is not None:
        ifds["gps"] += [(1, 2, "N" if lat >= 0 else "S"), (2, 5, rationals(abs(lat))),
                        (3, 2, "E" if lon >= 0 else "W"), (4, 5, rationals(abs(lon)))]
        ifds["ifd0"].append((0x8825, 4, "gps"))
    return build_tiff(e, {k: v for k, v in ifds.items() if v or k == "ifd0"})


def make_jpeg(tiff):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0"
    app1 = b"\xff\xe1" + struct.pack(">H", 2 + 6 + len(tiff)) + b"Exif\0\0" + tiff
    return b"\xff\xd8" + app0 + app1 + b"\xff\xda\x00\x02" + b"\x00" * 32


def _box(kind, payload):
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def make_heic(tiff, iloc_version=0, header_offset=6):
    """ftyp + meta(iinf, iloc) + mdat の最小構成。item 1 は画像のダミー、item 2 が Exif"""
    exif_item = struct.pack(">I", header_offset) + (b"Exif\0\0" if header_offset == 6 else b"\0" * header_offset) + tiff
    fake_image = b"\x11" * 16

    def meta_box(exif_off):
        infe = lambda i, t: _box(b"infe", b"\x02\0\0\0" + struct.pack(">HH", i, 0) + t + b"\0")
        iinf = _box(b"iinf", b"\0\0\0\0" + struct.pack(">H", 2) + infe(1, b"hvc1") + infe(2, b"Exif"))
        if iloc_version == 0:
            item = lambda i, off, ln: struct.pack(">HHHII", i, 0, 1, off, ln)
            iloc = _box(b"iloc", b"\0\0\0\0" + b"\x44\x00" + struct.pack(">H", 2)
                        + item(1, exif_off - len(fake_image), len(fake_image)) + item(2, exif_off, len(exif_item)))
        else:   # version 1: construction_method あり
            item = lambda i, off, ln: struct.pack(">HHHHII", i, 0, 0, 1, off, ln)
            iloc = _box(b"iloc", b"\x01\0\0\0" + b"\x44\x00" + struct.pack(">H", 2)
                        + item(1, exif_off - len(fake_image), len(fake_image)) + item(2, exif_off, len(exif_item)))
        return _box(b"meta", b"\0\0\0\0" + iinf + iloc)

    ftyp = _box(b"ftyp", b"heic\0\0\0\0mif1heic")
    meta_len = len(meta_box(len(fake_image)))   # 長さ計算用の仮のオフセット（実際の値は下で入れ直す）
    exif_off = len(ftyp) + meta_len + 8 + len(fake_image)
    return ftyp + meta_box(exif_off) + _box(b"mdat", fake_image + exif_item)


def write(dirpath, name, data):
    p = os.path.join(dirpath, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


class ReadPhotoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def read(self, name, data):
        return pe.read_photo(write(self.tmp.name, name, data))

    def test_jpeg_リトルエンディアン(self):
        r = self.read("a.jpg", make_jpeg(exif_tiff("<")))
        self.assertAlmostEqual(r["lat"], 35.681236, places=5)
        self.assertAlmostEqual(r["lon"], 139.767125, places=5)
        self.assertEqual(r["t"], "2024-05-03T14:23:11+09:00")

    def test_jpeg_ビッグエンディアン(self):
        r = self.read("a.jpg", make_jpeg(exif_tiff(">")))
        self.assertAlmostEqual(r["lat"], 35.681236, places=5)
        self.assertEqual(r["t"], "2024-05-03T14:23:11+09:00")

    def test_南緯と西経は負になる(self):
        r = self.read("a.jpg", make_jpeg(exif_tiff("<", lat=-33.8688, lon=-151.2093)))
        self.assertAlmostEqual(r["lat"], -33.8688, places=5)
        self.assertAlmostEqual(r["lon"], -151.2093, places=5)

    def test_オフセット無しは現地時刻のまま(self):
        r = self.read("a.jpg", make_jpeg(exif_tiff("<", offset=None)))
        self.assertEqual(r["t"], "2024-05-03T14:23:11")

    def test_GPSが無い写真はNone(self):
        self.assertIsNone(self.read("a.jpg", make_jpeg(exif_tiff("<", lat=None))))

    def test_日時が無い写真はNone(self):
        self.assertIsNone(self.read("a.jpg", make_jpeg(exif_tiff("<", dt=None))))

    def test_緯度経度0_0はNone(self):
        self.assertIsNone(self.read("a.jpg", make_jpeg(exif_tiff("<", lat=0.0, lon=0.0))))

    def test_日付が0埋めのダミーはNone(self):
        self.assertIsNone(self.read("a.jpg", make_jpeg(exif_tiff("<", dt="0000:00:00 00:00:00"))))

    def test_EXIFの無いJPEGやJPEGでないファイルはNone(self):
        self.assertIsNone(self.read("a.jpg", b"\xff\xd8\xff\xda\x00\x02" + b"\0" * 8))
        self.assertIsNone(self.read("b.jpg", b"not an image"))
        self.assertIsNone(self.read("c.jpg", b""))

    def test_heic_iloc_v0(self):
        r = self.read("a.heic", make_heic(exif_tiff("<")))
        self.assertAlmostEqual(r["lat"], 35.681236, places=5)
        self.assertAlmostEqual(r["lon"], 139.767125, places=5)
        self.assertEqual(r["t"], "2024-05-03T14:23:11+09:00")

    def test_heic_iloc_v1_ビッグエンディアン(self):
        r = self.read("a.heif", make_heic(exif_tiff(">"), iloc_version=1))
        self.assertAlmostEqual(r["lat"], 35.681236, places=5)

    def test_heic_Exifヘッダ長が0(self):
        r = self.read("a.heic", make_heic(exif_tiff("<"), header_offset=0))
        self.assertAlmostEqual(r["lon"], 139.767125, places=5)

    def test_壊れたheicはNone(self):
        data = make_heic(exif_tiff("<"))
        self.assertIsNone(self.read("a.heic", data[:40]))
        self.assertIsNone(self.read("b.heic", b"\0\0\0\x08ftyp"))


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jpeg = make_jpeg(exif_tiff("<"))

    def names(self, paths):
        return sorted(os.path.relpath(p, self.tmp.name) for p in paths)

    def test_再帰的に写真だけ拾い_NASの管理フォルダと隠しは除く(self):
        d = self.tmp.name
        write(d, "2024/a.jpg", self.jpeg)
        write(d, "2024/b.JPEG", self.jpeg)
        write(d, "2024/c.HEIC", self.jpeg)
        write(d, "2024/d.png", b"x")
        write(d, "2024/@eaDir/a.jpg/SYNOPHOTO_THUMB_XL.jpg", self.jpeg)   # Synology のサムネイル置き場
        write(d, "2024/.hidden/e.jpg", self.jpeg)
        write(d, "2024/._a.jpg", self.jpeg)                                   # macOS のリソースフォーク
        self.assertEqual(self.names(pe.iter_photos([d])), ["2024/a.jpg", "2024/b.JPEG", "2024/c.HEIC"])

    def test_ファイルを直接指定できる(self):
        p = write(self.tmp.name, "x/a.jpg", self.jpeg)
        self.assertEqual(list(pe.iter_photos([p])), [p])


class OutputTest(unittest.TestCase):
    def test_アプリのバックアップ形式になっている(self):
        payload = pe.build_payload([{"t": "2024-05-03T14:23:11+09:00", "lat": 35.5, "lon": 139.5}])
        self.assertEqual(payload["app"], "kitakoto")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["records"], [{"t": "2024-05-03T14:23:11+09:00", "te": None, "lat": 35.5, "lon": 139.5,
                                               "kind": "photo", "label": None}])

    def test_mainで出力ファイルができる(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "in/a.jpg", make_jpeg(exif_tiff("<")))
            write(d, "in/b.jpg", make_jpeg(exif_tiff("<", lat=None)))   # GPS無し → 除外
            out = os.path.join(d, "photos.json")
            self.assertEqual(pe.main([os.path.join(d, "in"), "-o", out]), 0)
            with open(out, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["records"][0]["kind"], "photo")

    def test_入力が存在しないときは異常終了(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertNotEqual(pe.main([os.path.join(d, "nothing"), "-o", os.path.join(d, "o.json")]), 0)


if __name__ == "__main__":
    unittest.main()
