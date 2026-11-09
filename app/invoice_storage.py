"""成本发票图片的落盘与校验。

为什么单独一个模块：web 端和小程序端都要往同一张表、同一个目录里写，
校验口径（多大、什么格式、文件名怎么起、删的时候怎么防越权）必须只有一份，
两边各写一遍迟早会有一边漏掉 2MB 校验 —— 那种漏洞在手机上根本看不出来。

设计取舍：
  * **只存路径不存二进制**。2MB 一张、一笔最多 9 张，塞进 MySQL 会把库撑大、
    让 deploy.sh 里的 mysqldump 越来越慢，而图片本来就该由文件系统管。
  * **不信扩展名也不信 Content-Type**，只看文件头。前端传 .jpg 里包着 exe、
    或者把 content-type 写成 image/jpeg 的，都过不去。
  * 文件名用 uuid4，不带原始文件名：原始名可能是 `发票-张伟.pdf` 这类，
    既是中文又要转义，还会在同一目录里撞名。
"""

from __future__ import annotations

import io
import re
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from PIL import Image

from app.config import settings

# 单张上限 2MB。与前端提示文案、小程序压缩逻辑是同一个口径，
# 改这里必须同步改 CostManage.vue / 小程序 costManage 页的文案。
MAX_INVOICE_BYTES = 2 * 1024 * 1024

# 一笔成本最多挂 9 张：专票 + 销货清单 + 付款凭证也够，同时挡住手滑传几十张。
MAX_INVOICES_PER_COST = 9

# 像素上限。2MB 的 PNG 可以解成 8000×8000（约 190MB 内存），
# 手机原图一般 1200 万像素（12MP），40MP 已经给足余量。
MAX_INVOICE_PIXELS = 40_000_000

INVOICE_DIR = "invoices"


def _root() -> Path:
    """上传根目录。相对路径按项目根（app 包的上一级）解析，不用 cwd。"""
    base = Path(settings.upload_dir).expanduser()
    if not base.is_absolute():
        base = Path(__file__).resolve().parent.parent / base
    return base


def ensure_root() -> Path:
    """确保根目录存在并返回它。

    StaticFiles 在启动时就会 stat 这个目录，不存在会直接让整个应用起不来。
    首次部署、卷还没建出来的时候必须自己先建。
    """
    root = _root()
    (root / INVOICE_DIR).mkdir(parents=True, exist_ok=True)
    return root


def _sniff(data: bytes) -> tuple[str, str] | None:
    """按文件头判断真实类型，返回 (扩展名, MIME)。识别不了就返回 None。"""
    if data[:3] == b"\xff\xd8\xff":
        return ("jpg", "image/jpeg")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ("png", "image/png")
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("webp", "image/webp")
    return None


def _safe_filename(name: str) -> str:
    """只留给人看的原始名：去掉路径分隔符与控制字符，截到 200 字符。"""
    cleaned = re.sub(r"[\\/\x00-\x1f\x7f]", "", (name or "").strip())
    return cleaned[:200] or "未命名图片"


def save_invoice_image(data: bytes, original_name: str = "") -> dict:
    """校验并保存一张发票图，返回可直接写进 cost_invoices 的字段。

    校验顺序刻意是「空 → 大小 → 类型」：先报大小能省掉一次图片解码，
    而且用户最需要看到的就是"你这张太大了"。
    """
    if not data:
        raise HTTPException(400, "上传的文件是空的，请重新选择图片")

    if len(data) > MAX_INVOICE_BYTES:
        mb = len(data) / 1024 / 1024
        raise HTTPException(
            413,
            f"这张图 {mb:.1f}MB，超过单张 2MB 的上限。"
            "请在手机上重新拍一张（相机会自动压缩），或先截屏再上传。",
        )

    kind = _sniff(data)
    if kind is None:
        raise HTTPException(
            400,
            "只支持 JPG / PNG / WebP 图片。"
            "如果这是电子发票（PDF），请先截屏保存成图片再上传。",
        )
    ext, content_type = kind

    # 文件头对了还不够：改个扩展名、或者传一个只有 PNG 头的半截文件，
    # 落库后前端就是一张裂图，谁也说不出原因。这里真解一次结构。
    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
            probe.verify()
    except Exception as exc:  # noqa: BLE001 - Pillow 的异常类型太多，统一拒绝
        raise HTTPException(
            400, "这张图打不开（文件可能已损坏或不是真正的图片），请重新拍照或截图上传"
        ) from exc

    if width * height > MAX_INVOICE_PIXELS:
        raise HTTPException(
            400,
            f"图片分辨率 {width}×{height} 太大了，请用手机直接拍摄原图"
            "（不要上传扫描件原图或从相册二次导出的大图）",
        )

    now = datetime.now()
    rel_dir = Path(INVOICE_DIR) / f"{now:%Y}" / f"{now:%m}"
    target_dir = _root() / rel_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{uuid.uuid4().hex}.{ext}"
    (target_dir / filename).write_bytes(data)

    return {
        "file_path": (rel_dir / filename).as_posix(),
        "filename": _safe_filename(original_name),
        "size": len(data),
        "content_type": content_type,
    }


def resolve(rel_path: str) -> Path | None:
    """把库里的相对路径还原成绝对路径，并确保它没跑出上传根目录。

    路径来自数据库，正常不会被构造；但删文件是不可逆操作，
    这里多一层 realpath 判断，代价是一次 stat，值。
    """
    if not rel_path:
        return None
    root = _root().resolve()
    target = (root / rel_path).resolve()
    if root != target and root not in target.parents:
        return None
    return target


def delete_invoice_file(rel_path: str) -> bool:
    """删磁盘文件。文件已经不在了也算成功 —— 调用方不该因为清理失败而回滚业务。"""
    path = resolve(rel_path)
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def url_of(rel_path: str) -> str:
    """对外访问地址。走 /api 前缀是为了复用现有的反代，不用再给 /uploads 单独开规则。"""
    return f"/api/uploads/{rel_path}" if rel_path else ""
