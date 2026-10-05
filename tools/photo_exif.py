#!/usr/bin/env python3
"""写真のEXIFから撮影位置と撮影日時を取り出し、「来たことある？」に読み込めるJSONを書き出す。

標準ライブラリだけで動く。JPEG と HEIC/HEIF に対応。
使い方:
    python3 photo_exif.py 写真フォルダ [写真フォルダ ...] -o photos.json
できた photos.json を、アプリの設定 → 「タイムラインを読み込む」で Timeline.json などと一緒に選ぶ。
位置情報つきの写真は、アプリ側で「写真 N枚」として区別して表示される（kind=photo）。
"""
import argparse
import json
import os
import re
import struct
import sys
from datetime import datetime, timezone

PHOTO_EXTS = {".jpg", ".jpeg", ".heic", ".heif"}
BACKUP_APP = "kitakoto"   # index.html の BACKUP_APP と揃える
MAX_META = 32 * 1024 * 1024   # HEIC の meta ボックスを読む上限

# EXIF のタグ
TAG_DATETIME, TAG_EXIF_IFD, TAG_GPS_IFD = 0x0132, 0x8769, 0x8825
TAG_DT_ORIGINAL, TAG_DT_DIGITIZED = 0x9003, 0x9004
# 日時タグごとの、タイムゾーン差（"+09:00"）を持つタグ
OFFSET_TAG = {TAG_DT_ORIGINAL: 0x9011, TAG_DT_DIGITIZED: 0x9012, TAG_DATETIME: 0x9010}
TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def _u(buf, pos, n):
    """big-endian の整数を読む。足りなければ ValueError（壊れたファイルを黙って読み違えない）"""
    if n == 0:
        return 0
    if pos < 0 or pos + n > len(buf):
        raise ValueError("データが足りない")
    return int.from_bytes(buf[pos:pos + n], "big")


# ---------- TIFF（EXIF 本体） ----------
def _read_ifd(tiff, e, off):
    """IFD を {tag: (type, count, 値フィールドの位置)} にする"""
    (n,) = struct.unpack_from(e + "H", tiff, off)
    out = {}
    for i in range(n):
        pos = off + 2 + 12 * i
        tag, typ, count = struct.unpack_from(e + "HHI", tiff, pos)
        out[tag] = (typ, count, pos + 8)
    return out


def _value_pos(tiff, e, entry):
    typ, count, field = entry
    size = TYPE_SIZE.get(typ, 1) * count
    return field if size <= 4 else struct.unpack_from(e + "I", tiff, field)[0]


def _ascii(tiff, e, entry):
    typ, count, _ = entry
    pos = _value_pos(tiff, e, entry)
    if pos + count > len(tiff):
        raise ValueError("データが足りない")
    return tiff[pos:pos + count].split(b"\0", 1)[0].decode("ascii", "replace").strip()


def _degrees(tiff, e, entry):
    """度・分・秒の有理数3つ → 十進の度"""
    typ, count, _ = entry
    if typ != 5 or count != 3:
        raise ValueError("緯度経度の形式が違う")
    pos = _value_pos(tiff, e, entry)
    v = []
    for i in range(3):
        n, d = struct.unpack_from(e + "II", tiff, pos + 8 * i)
        if d == 0:
            raise ValueError("分母が0")
        v.append(n / d)
    return v[0] + v[1] / 60 + v[2] / 3600


def _datetime(tiff, e, entry, offset_entry):
    m = re.match(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", _ascii(tiff, e, entry))
    if not m:
        return None
    y, mo, d, h, mi, s = map(int, m.groups())
    try:
        datetime(y, mo, d, h, mi, s)   # 0000:00:00 などのダミー値を除く
    except ValueError:
        return None
    t = f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}"
    if offset_entry:
        off = _ascii(tiff, e, offset_entry)
        if re.fullmatch(r"[+-]\d{2}:\d{2}", off):
            t += off
    return t


def _parse_tiff(tiff):
    e = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if not e:
        return None
    ifd0 = _read_ifd(tiff, e, struct.unpack_from(e + "I", tiff, 4)[0])
    if TAG_GPS_IFD not in ifd0:
        return None
    gps = _read_ifd(tiff, e, struct.unpack_from(e + "I", tiff, ifd0[TAG_GPS_IFD][2])[0])
    if not all(k in gps for k in (1, 2, 3, 4)):
        return None
    lat, lon = _degrees(tiff, e, gps[2]), _degrees(tiff, e, gps[4])
    if _ascii(tiff, e, gps[1]).upper() == "S":
        lat = -lat
    if _ascii(tiff, e, gps[3]).upper() == "W":
        lon = -lon
    if abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0):
        return None

    exif = _read_ifd(tiff, e, struct.unpack_from(e + "I", tiff, ifd0[TAG_EXIF_IFD][2])[0]) if TAG_EXIF_IFD in ifd0 else {}
    t = None
    for tag, ifd in ((TAG_DT_ORIGINAL, exif), (TAG_DT_DIGITIZED, exif), (TAG_DATETIME, ifd0)):
        if tag in ifd:
            t = _datetime(tiff, e, ifd[tag], ifd.get(OFFSET_TAG[tag]) or exif.get(OFFSET_TAG[tag]))
            if t:
                break
    return {"t": t, "lat": round(lat, 6), "lon": round(lon, 6)} if t else None


# ---------- コンテナから TIFF を取り出す ----------
def _jpeg_tiff(f):
    f.seek(2)
    while True:
        b = f.read(1)
        if not b:
            return None
        if b != b"\xff":
            continue
        marker = f.read(1)
        while marker == b"\xff":   # 詰め物の 0xFF
            marker = f.read(1)
        if not marker or marker in (b"\xd9", b"\xda"):   # EOI / SOS（画像データ）まで来たら終わり
            return None
        if marker == b"\x01" or b"\xd0" <= marker <= b"\xd8":   # 長さを持たないマーカー
            continue
        raw = f.read(2)
        if len(raw) < 2:
            return None
        length = struct.unpack(">H", raw)[0] - 2
        if marker == b"\xe1":
            data = f.read(length)
            if data[:6] == b"Exif\0\0":
                return data[6:]
        else:
            f.seek(length, 1)


def _boxes(buf, pos=0):
    """ISOボックスを (種類, 中身の開始, 終了) で列挙する"""
    while pos + 8 <= len(buf):
        size, kind = struct.unpack_from(">I4s", buf, pos)
        head = 8
        if size == 1:
            size, head = _u(buf, pos + 8, 8), 16
        elif size == 0:
            size = len(buf) - pos
        if size < head:
            return
        yield kind, pos + head, min(pos + size, len(buf))
        pos += size


def _heic_exif_extents(meta):
    """meta の中身から、Exif アイテムの (ファイル先頭からの位置, 長さ) の一覧を返す"""
    exif_ids, iloc = set(), None
    for kind, s, end in _boxes(meta, 4):   # 先頭4バイトは version/flags
        if kind == b"iinf":
            v = meta[s]
            p = s + 4 + (2 if v == 0 else 4)
            for k2, s2, _ in _boxes(meta, p):
                if k2 == b"infe" and meta[s2] >= 2:
                    v2 = meta[s2]
                    id_len = 2 if v2 == 2 else 4
                    if meta[s2 + 4 + id_len + 2:s2 + 4 + id_len + 6] == b"Exif":
                        exif_ids.add(_u(meta, s2 + 4, id_len))
        elif kind == b"iloc":
            iloc = (s, end)
    if not iloc or not exif_ids:
        return []
    s, _ = iloc
    v = meta[s]
    off_size, len_size = meta[s + 4] >> 4, meta[s + 4] & 15
    base_size, idx_size = meta[s + 5] >> 4, (meta[s + 5] & 15 if v in (1, 2) else 0)
    p = s + 6
    count = _u(meta, p, 4 if v == 2 else 2)
    p += 4 if v == 2 else 2
    found = []
    for _ in range(count):
        item_id = _u(meta, p, 4 if v == 2 else 2)
        p += 4 if v == 2 else 2
        method = 0
        if v in (1, 2):
            method = _u(meta, p, 2) & 15
            p += 2
        p += 2   # data_reference_index
        base = _u(meta, p, base_size)
        p += base_size
        n_ext = _u(meta, p, 2)
        p += 2
        extents = []
        for _ in range(n_ext):
            p += idx_size
            extents.append((base + _u(meta, p, off_size), _u(meta, p + off_size, len_size)))
            p += off_size + len_size
        if item_id in exif_ids and method == 0:
            found += extents
    return found


def _heic_tiff(f):
    f.seek(0, 2)
    total = f.tell()
    pos = 0
    while pos + 8 <= total:
        f.seek(pos)
        size, kind = struct.unpack(">I4s", f.read(8))
        head = 8
        if size == 1:
            size, head = struct.unpack(">Q", f.read(8))[0], 16
        elif size == 0:
            size = total - pos
        if size < head:
            return None
        if kind == b"meta":
            meta = f.read(min(size - head, MAX_META))
            payload = b""
            for off, length in _heic_exif_extents(meta):
                f.seek(off)
                chunk = f.read(length)
                if len(chunk) < length:
                    return None
                payload += chunk
            if len(payload) < 4:
                return None
            tiff = payload[4 + _u(payload, 0, 4):]
            return tiff if tiff[:2] in (b"II", b"MM") else None
        pos += size
    return None


def read_photo(path):
    """写真1枚から {"t", "lat", "lon"} を返す。位置か日時が無い・読めない場合は None"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:2] == b"\xff\xd8":
                tiff = _jpeg_tiff(f)
            elif head[4:8] == b"ftyp":
                tiff = _heic_tiff(f)
            else:
                return None
        return _parse_tiff(tiff) if tiff else None
    except (OSError, ValueError, IndexError, struct.error):
        return None


# ---------- 走査と出力 ----------
def _skip_name(name):
    # 隠しファイル、macOS の ._xxx、Synology の @eaDir（サムネイル置き場）・#recycle（ごみ箱）
    return name[:1] in (".", "@", "#")


def iter_photos(paths):
    for root in paths:
        if os.path.isfile(root):
            if os.path.splitext(root)[1].lower() in PHOTO_EXTS:
                yield root
            continue
        for d, dirs, files in os.walk(root):
            dirs[:] = sorted(x for x in dirs if not _skip_name(x))
            for name in sorted(files):
                if not _skip_name(name) and os.path.splitext(name)[1].lower() in PHOTO_EXTS:
                    yield os.path.join(d, name)


def build_payload(items):
    """アプリのバックアップ形式（index.html の backupPayload と同じ）にする。写真は kind=photo（アプリが写真由来と分かるようにする）"""
    records = [{"t": i["t"], "te": None, "lat": i["lat"], "lon": i["lon"], "kind": "photo", "label": None} for i in items]
    return {"app": BACKUP_APP, "version": 1, "exportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(records), "records": records}


def main(argv=None):
    ap = argparse.ArgumentParser(description="写真のEXIFの位置情報を「来たことある？」用のJSONにする")
    ap.add_argument("paths", nargs="+", help="写真のフォルダ（再帰的に探す）またはファイル")
    ap.add_argument("-o", "--output", default="photos.json", help="出力ファイル（既定: photos.json）")
    args = ap.parse_args(argv)

    missing = [p for p in args.paths if not os.path.exists(p)]
    if missing:
        print("見つかりません: " + ", ".join(missing), file=sys.stderr)
        return 2

    items, scanned = [], 0
    for path in iter_photos(args.paths):
        scanned += 1
        r = read_photo(path)
        if r:
            items.append(r)
        if scanned % 500 == 0 and sys.stderr.isatty():
            print(f"\r{scanned} 枚を確認中…", end="", file=sys.stderr)
    if sys.stderr.isatty():
        print("\r" + " " * 30 + "\r", end="", file=sys.stderr)

    print(f"写真 {scanned} 枚のうち、位置情報つきは {len(items)} 枚", file=sys.stderr)
    if not items:
        print("位置情報つきの写真が無かったので、ファイルは作りませんでした。", file=sys.stderr)
        return 1
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(build_payload(items), f, ensure_ascii=False, separators=(",", ":"))
    print(f"書き出しました: {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
