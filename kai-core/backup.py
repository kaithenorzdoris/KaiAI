# -*- coding: utf-8 -*-
"""
Backup tự động — cứu dữ liệu khi Render deploy lại container.

Vì sao cần
----------
Render dùng **ephemeral filesystem**: mỗi lần deploy, container cũ bị xoá và
container mới khởi động với ổ trống. Mọi ``.db``/``.json`` trong
``KAI_DATA_DIR`` biến mất sạch.

Cách chữa
---------
Giữ nguyên SQLite (nhanh, đơn giản) nhưng **đẩy ảnh chụp dữ liệu ra nơi lưu
trữ bền vững bên ngoài**, rồi **tự kéo về lúc khởi động**::

    khoi dong   ->  pull()  ->  bot chay voi du lieu cu
    moi N phut ->  push()  ->  anh moi nhat nam tren R2

Cau hinh (bien moi truong)::

    KAI_BACKUP_URL=https://<account>.r2.cloudflarestorage.com/<bucket>
    KAI_BACKUP_ACCESS_KEY_ID=<Access Key ID>
    KAI_BACKUP_SECRET_ACCESS_KEY=<Secret Access Key>
    KAI_BACKUP_REGION=auto             # tuy chon: "auto" cho R2 (mac dinh);
                                       # tu nhan dien neu bo trong; B2/S3 bat
                                       # buoc region that, vd "us-west-004"
    KAI_BACKUP_PREFIX=kaiai          # ten khoa tren R2 (mac dinh "kaiai")
    KAI_BACKUP_INTERVAL_MIN=30       # chu ky backup (mac dinh 30 phut)
    KAI_BACKUP_KEEP=5               # so ban xoay vong (mac dinh 5)

Nha cung cap duoc ho tro: Cloudflare R2 (region ``auto``) va Backblaze B2
(region that nhu ``us-west-004``, tu nhan dien tu URL) — ca hai deu la API
tuong thich S3, ky AWS Signature V4.

Module nay chi dung ``aiohttp`` (da co san) + ``zipfile`` cua thu vien chuan.

An toan du lieu
---------------
* ``sqlite3.Connection.backup()`` tao ban sao nhat quan **ke ca khi bot dang
  ghi** — khong can dung bot, khong sinh file hong.
* Pull co kiem tra tinh toan ven cua zip truoc khi giai nen; hong thi bo qua
  va canh bao thay vi lam hong du lieu dang co.
"""

from __future__ import annotations

import aiohttp
import hashlib
import hmac
import io
import os
import re
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("backup")

#: Cac duoi file duoc sao luu. Chi ``.db``/``.json`` — log xoay vong va file
#: backup cu khong can (va se lam nang).
INCLUDE_SUFFIXES = (".db", ".json")

#: Khong bao gio gom cac file nay (log xoay vong gay nhieu).
EXCLUDE_NAMES = ("kai.log",)


@dataclass(slots=True)
class BackupSettings:
    """Cau hinh backup doc tu bien moi truong."""

    url: str = ""
    access_key: str = ""
    secret_key: str = ""
    prefix: str = "kaiai"
    region: str = ""
    interval_min: int = 30
    keep: int = 5

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.access_key and self.secret_key)

    @property
    def resolved_region(self) -> str:
        """Region SigV4 thuc te: uu tien KAI_BACKUP_REGION, fallback tu URL."""
        if self.region:
            return self.region
        return detect_region(self.url)

    @property
    def key(self) -> str:
        """Ten file anh moi nhat (prefix do ``_endpoint`` them vao)."""
        return "latest.zip"

    def stamped_key(self, stamp: str) -> str:
        """Ten file ban lich su de xoay vong."""
        return f"{self.prefix}-{stamp}.zip"


#: Host R2 dung region co dinh ``auto`` trong SigV4.
R2_HOST_SUFFIX = ".r2.cloudflarestorage.com"

#: Backblaze B2 dang ``s3.<region>.backblazeb2.com`` — region that phai di vao
#: chu ky, dung ``auto`` se bi 403 Forbidden.
_B2_REGION_RE = re.compile(r"s3\.([a-z0-9-]+)\.backblazeb2\.com$", re.IGNORECASE)

#: AWS S3 that dang ``s3.<region>.amazonaws.com`` / ``s3-<region>...``.
_AWS_REGION_RE = re.compile(
    r"s3[.-]([a-z0-9-]+)\.amazonaws\.com$", re.IGNORECASE
)


def detect_region(url: str) -> str:
    """Doan region SigV4 tu host cua ``KAI_BACKUP_URL``.

    * R2 (``*.r2.cloudflarestorage.com``) -> ``"auto"``.
    * B2 (``s3.<region>.backblazeb2.com``) -> ``"<region>"``.
    * S3 that (``s3.<region>.amazonaws.com``) -> ``"<region>"``.
    * Con lai -> ``"auto"`` (tuong thich nguoc voi cau hinh R2 cu).
    """
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return R2_REGION
    if not host:
        return R2_REGION
    if host.endswith(R2_HOST_SUFFIX):
        return R2_REGION
    m = _B2_REGION_RE.search(host)
    if m:
        return m.group(1).lower()
    m = _AWS_REGION_RE.search(host)
    if m:
        return m.group(1).lower()
    return R2_REGION


def _int_env(name: str, default: int, *, minimum: int) -> int:
    """Doc int tu moi truong; gia tri hong -> dung ``default``."""
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def load_settings() -> BackupSettings:
    """Doc cau hinh tu ``os.environ`` voi gia tri mac dinh an toan."""
    return BackupSettings(
        url=os.environ.get("KAI_BACKUP_URL", "").strip(),
        access_key=os.environ.get("KAI_BACKUP_ACCESS_KEY_ID", "").strip(),
        secret_key=os.environ.get("KAI_BACKUP_SECRET_ACCESS_KEY", "").strip(),
        prefix=(os.environ.get("KAI_BACKUP_PREFIX", "kaiai") or "kaiai").strip(),
        region=(os.environ.get("KAI_BACKUP_REGION", "") or "").strip().lower(),
        interval_min=_int_env("KAI_BACKUP_INTERVAL_MIN", 30, minimum=1),
        keep=_int_env("KAI_BACKUP_KEEP", 5, minimum=1),
    )


def collect_files(data_dir: Path) -> list[Path]:
    """Liet ke file can backup trong ``data_dir`` (khong de quy subfolder)."""
    out: list[Path] = []
    for path in sorted(data_dir.glob("*")):
        if not path.is_file():
            continue
        if path.name.startswith("KaiAI_backup_"):
            continue          # tranh gom ban backup cu vao ban moi
        if path.name in EXCLUDE_NAMES:
            continue
        if path.suffix.lower() not in INCLUDE_SUFFIXES:
            continue
        out.append(path)
    return out


def _snapshot_db(src: Path) -> bytes:
    """Tra ve noi dung ``.db`` da **checkpoint nhat quan**.

    Dung API ``sqlite3.Connection.backup()`` nen van dung khi bot dang ghi:
    khong can dung bot, khong tao file hong, va WAL duoc gop lai dung cach.
    """
    import sqlite3
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        copy = Path(tmp_dir) / "snapshot.db"
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            dest = sqlite3.connect(str(copy))
            try:
                conn.backup(dest)          # bam chup nhat quan
            finally:
                dest.close()
        finally:
            conn.close()
        return copy.read_bytes()



def compute_sha256(data: bytes) -> str:
    """Tính toán mã băm SHA-256 của khối dữ liệu sao lưu."""
    return hashlib.sha256(data).hexdigest()


def validate_sqlite_integrity(blob: bytes) -> bool:
    """Xác thực tính toàn vẹn của file SQLite trước khi ghi đè vào ổ đĩa."""
    import sqlite3
    if len(blob) < 100 or blob[:16] != b"SQLite format 3\x00":
        return False
    try:
        conn = sqlite3.connect(":memory:")
        if hasattr(conn, "deserialize"):
            conn.deserialize(blob)
            cur = conn.execute("PRAGMA integrity_check;")
            row = cur.fetchone()
            conn.close()
            return bool(row and row[0] == "ok")
        conn.close()
        return True
    except Exception:
        return False

def build_archive(data_dir: Path, files: list[Path]) -> bytes:
    """Nen cac file da chon thanh mot zip trong RAM (tra ve bytes)."""
    buffer = io.BytesIO()
    # ``ZIP_DEFLATED`` giam kich thuoc ~mot nua so voi luu thang SQLite.
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            try:
                if path.suffix.lower() == ".db":
                    zf.writestr(path.name, _snapshot_db(path))
                else:
                    zf.writestr(path.name, path.read_bytes())
            except Exception as exc:  # noqa: BLE001 - 1 file hong khong
                # duoc lam hong ca ban backup.
                log.warning("Bo qua %s khi backup: %s", path.name, exc)
    return buffer.getvalue()


def extract_archive(blob: bytes, data_dir: Path) -> list[str]:
    """Giai nen zip vao ``data_dir`` va tra ve danh sach file da khoi phuc.

    Chan path traversal (``../``): archive tu nguon ngoai khong duoc ghi file
    ra ngoai thu muc du lieu.
    """
    restored: list[str] = []
    base = data_dir.resolve()
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        # Gom về ValueError de caller bat bang mot kieu loi duy nhat.
        raise ValueError(f"Khong doc duoc file zip: {exc}") from exc
    with archive as zf:
        broken = zf.testzip()
        if broken is not None:
            raise ValueError(f"Zip hong tai {broken}")
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name      # cat path traversal
            if not name or name.startswith("."):
                continue
            target = (base / name).resolve()
            if target.parent != base:
                continue
            blob_data = zf.read(info)
            if name.lower().endswith(".db") and not validate_sqlite_integrity(blob_data):
                raise ValueError(f"File database '{name}' trong bản sao lưu bị hỏng (integrity check failed).")
            target.write_bytes(blob_data)
            restored.append(name)
    return restored


def _endpoint(settings: BackupSettings, key: str) -> str:
    """Dựng URL đầy đủ trên R2 (S3-compatible) từ tên file trong khoá."""
    base = settings.url.rstrip("/")
    return f"{base}/{settings.prefix}/{key}"


# --------------------------------------------------------------------------
# AWS Signature Version 4 — R2 BẮT BUỘC dùng chữ ký này.
#
# Endpoint ``https://<account>.r2.cloudflarestorage.com`` là API tương
# thích S3, nên KHÔNG chấp nhận header ``Authorization: Bearer <token>``.
# Phải ký bằng Access Key ID + Secret Access Key (tức làm đúng AWS SigV4)
# nên module này tự ký — khỏi phải thêm boto3/minio vào requirements.
# --------------------------------------------------------------------------

#: R2 báo region cố định là ``auto``.
R2_REGION = "auto"
#: Tên dịch vụ trong SigV4 (S3-compatible).
S3_SERVICE = "s3"
#: Thuật toán chữ ký.
ALGORITHM = "AWS4-HMAC-SHA256"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(
    secret: str, date_stamp: str, region: str = R2_REGION, service: str = S3_SERVICE
) -> bytes:
    """Suy ra khoá ký từ Secret Access Key (chuỗi HMAC 4 bậc)."""
    key = _hmac(f"AWS4{secret}".encode("utf-8"), date_stamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def sigv4_headers(
    method: str,
    url: str,
    payload: bytes,
    access_key: str,
    secret_key: str,
    *,
    timestamp: str | None = None,
    region: str = R2_REGION,
    service: str = S3_SERVICE,
    include_content_hash: bool = True,
) -> dict[str, str]:
    """Trả về header đã ký SigV4 cho một lệnh HTTP tới R2.

    ``timestamp`` là chuỗi ``YYYYMMDDTHHMMSSZ`` (dùng UTC). Cho phép truyền
    vào cùng ``region``/``service``/``include_content_hash`` để test với
    vector chữ ký chuẩn của AWS.
    """
    stamp = timestamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    date_stamp = stamp[:8]
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc
    # Canonical URI: đường dẫn đã encode, giữ nguyên dấu "/".
    canonical_uri = parsed.path or "/"
    payload_hash = _sha256_hex(payload)

    headers = {"host": host, "x-amz-date": stamp}
    if include_content_hash:
        headers["x-amz-content-sha256"] = payload_hash
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(
        f"{k}:{headers[k]}\n" for k in sorted(headers)
    )
    canonical_request = "\n".join(
        [method.upper(), canonical_uri, "", canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            ALGORITHM,
            stamp,
            scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers["authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


async def push(
    settings: BackupSettings,
    data_dir: Path,
    *,
    session: aiohttp.ClientSession | None = None,
) -> bool:
    """Dong goi du lieu len kho luu tru.

    Ghi 2 khoa: ``latest.zip`` (luon tai, dung luc khoi dong) va mot ban co
    danh thoi gian de xoay vong. Tra ``True`` neu thanh cong.
    """
    if not settings.enabled:
        return False
    files = collect_files(data_dir)
    if not files:
        log.info("Khong co file du lieu nao de backup.")
        return False

    payload = build_archive(data_dir, files)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    own = session is None
    session = session or aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120)
    )
    try:
        for key in (settings.key, settings.stamped_key(stamp)):
            url = _endpoint(settings, key)
            signed = sigv4_headers(
                "PUT", url, payload, settings.access_key, settings.secret_key,
                region=settings.resolved_region,
            )
            signed.pop("host", None)   # aiohttp tự đặt Host
            signed["Content-Type"] = "application/zip"
            async with session.put(url, data=payload, headers=signed) as resp:
                if resp.status not in (200, 201):
                    log.warning(
                        "Backup len %s that bai: HTTP %s", key, resp.status
                    )
                    return False
        log.info(
            "Backup thanh cong: %d file, %s KB",
            len(files), len(payload) // 1024,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - backup fail khong duoc
        # lam bot chet.
        log.warning("Backup that bai: %s", exc)
        return False
    finally:
        if own:
            await session.close()


async def pull(
    settings: BackupSettings,
    data_dir: Path,
    *,
    session: aiohttp.ClientSession | None = None,
) -> list[str]:
    """Tai anh backup moi nhat ve ``data_dir``.

    Tra ve danh sach file da khoi phuc (rong neu khong co gi). Zip hong thi
    **khong** ghi de du lieu dang co.
    """
    if not settings.enabled:
        return []
    own = session is None
    session = session or aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120)
    )
    try:
        url = _endpoint(settings, settings.key)
        signed = sigv4_headers(
            "GET", url, b"", settings.access_key, settings.secret_key,
            region=settings.resolved_region,
        )
        signed.pop("host", None)   # aiohttp tự đặt Host
        async with session.get(url, headers=signed) as resp:
            if resp.status == 404:
                log.info("Chua co ban backup nao tren kho (deploy lan dau).")
                return []
            if resp.status != 200:
                log.warning("Tai backup that bai: HTTP %s", resp.status)
                return []
            blob = await resp.read()
        data_dir.mkdir(parents=True, exist_ok=True)
        restored = extract_archive(blob, data_dir)
        log.info(
            "Da khoi phuc %d file (%s KB): %s",
            len(restored), len(blob) // 1024, ", ".join(restored) or "-",
        )
        return restored
    except Exception as exc:  # noqa: BLE001 - khong duoc chet vi backup
        log.warning("Khong khoi phuc duoc du lieu: %s", exc)
        return []
    finally:
        if own:
            await session.close()


__all__ = (
    "BackupSettings",
    "build_archive",
    "collect_files",
    "detect_region",
    "extract_archive",
    "load_settings",
    "pull",
    "push",
)