"""
common.py - 跨网文本传输协议公用模块

数据流:
  文档 → 提取内容块 → JSON序列化 → zlib压缩 → Base64编码 → 分块 → QR码
  QR码 → 解析块 → 排序拼接 → Base64解码 → zlib解压 → JSON反序列化 → 重建文档

QR码载荷格式:
  {"v":1,"t":10,"i":0,"n":"doc.docx","s":15000,"d":"base64块数据..."}
  v: 协议版本  t: 总块数  i: 当前块索引  n: 文件名  s: 总压缩大小  d: 块数据
"""

import json
import zlib
import base64
import os
import zipfile
from io import BytesIO
from xml.etree import cElementTree as ET

# ── 协议常量 ──
PROTOCOL_VERSION = 1
DEFAULT_CHUNK_SIZE = 1200        # 每块字符数（base64串长度，平衡装载量与扫码可靠性）
DEFAULT_ENCODING = 'utf-8'

# ── QR 码 Version 40 各纠错级别最大容量（字节模式）──
# 用于防止生成超出 QR 码最大版本的数据
QR_MAX_PAYLOAD_BYTES = {
    "L": 2953,
    "M": 2331,
    "Q": 1663,
    "H": 1273,
}
QR_ENVELOPE_OVERHEAD = 150  # JSON 信封额外开销上限（含文件名中文）

def get_safe_chunk_size(ec_level):
    """根据纠错级别返回安全的块数据最大字符数"""
    max_bytes = QR_MAX_PAYLOAD_BYTES.get(ec_level, 2331)
    return max(500, max_bytes - QR_ENVELOPE_OVERHEAD)

# ── XML命名空间（用于docx解析）──
NSMAP = {
    'w':  'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'r':  'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'a':  'http://schemas.openxmlformats.org/drawingml/2006/main',
    'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture',
    'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
}
REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


# ══════════════════════════════════════════════════
# 1. docx / txt 文件读取 → 内容块列表
# ══════════════════════════════════════════════════

def extract_docx_content(filepath):
    """读取 docx 文件，按顺序提取文本和图片，返回内容块列表。

    每块格式: {"type": "text", "data": "..."} 或 {"type": "image", "data": "base64...", "ext": "png"}
    """
    blocks = []
    try:
        with zipfile.ZipFile(filepath) as z:
            # ── 读文档体 ──
            doc_xml = z.read('word/document.xml')
            root = ET.fromstring(doc_xml)

            # ── 读关系表 ──
            rel_map = {}
            try:
                rels_xml = z.read('word/_rels/document.xml.rels')
                for rel in ET.fromstring(rels_xml):
                    rid = rel.get('Id')
                    target = rel.get('Target')
                    if rid and target:
                        rel_map[rid] = target
            except KeyError:
                pass

            # ── 提取所有媒体文件 ──
            images = {}
            for name in z.namelist():
                if name.startswith('word/media/'):
                    images[name] = z.read(name)

            # ── 遍历 body 子元素 ──
            body = root.find('.//w:body', NSMAP) or root
            for elem in body.iter():
                tag = _localname(elem)
                if tag == 'p':
                    para_text, para_imgs = _parse_paragraph(elem, rel_map, images)
                    if para_text.strip():
                        blocks.append({"type": "text", "data": para_text.strip()})
                    blocks.extend(para_imgs)
                elif tag == 'tbl':
                    # 表格：提取所有单元格文本
                    for cell in elem.iter():
                        if _localname(cell) == 't' and cell.text:
                            blocks.append({"type": "text", "data": cell.text.strip()})
    except Exception as e:
        blocks = [{"type": "text", "data": f"[读取docx出错: {e}]"}]
    if not blocks:
        blocks = [{"type": "text", "data": ""}]
    return blocks


def _parse_paragraph(p_elem, rel_map, images):
    """解析一个段落元素，提取文本和图片。"""
    text_parts = []
    img_blocks = []
    for child in p_elem.iter():
        tag = _localname(child)
        if tag == 't' and child.text:
            text_parts.append(child.text)
        elif tag == 'drawing':
            for blip in child.iter():
                if _localname(blip) == 'blip':
                    embed_id = blip.get(f'{{{REL_NS}}}embed')
                    if embed_id and embed_id in rel_map:
                        img_path = 'word/' + rel_map[embed_id]
                        if img_path in images:
                            raw = images[img_path]
                            ext = (os.path.splitext(img_path)[1] or '.png').lstrip('.')
                            img_blocks.append({
                                "type": "image",
                                "data": base64.b64encode(raw).decode('ascii'),
                                "ext": ext
                            })
    return ''.join(text_parts), img_blocks


def read_txt_content(filepath):
    """读取 txt 文件，返回内容块列表。"""
    try:
        with open(filepath, 'r', encoding=DEFAULT_ENCODING) as f:
            text = f.read()
    except UnicodeDecodeError:
        with open(filepath, 'r', encoding='gbk') as f:
            text = f.read()
    if not text.strip():
        text = ""
    return [{"type": "text", "data": text}]


# ══════════════════════════════════════════════════
# 2. 内容块 → 序列化 → 压缩 → Base64 → 分块
# ══════════════════════════════════════════════════

def serialize_content(blocks, filename="", content_type=""):
    """内容块列表 → JSON 字符串"""
    doc = {
        "v": PROTOCOL_VERSION,
        "type": content_type or os.path.splitext(filename)[1].lstrip('.'),
        "filename": os.path.basename(filename),
        "content": blocks,
    }
    return json.dumps(doc, ensure_ascii=False, separators=(',', ':'))


def compress_encode(data_str):
    """JSON 字符串 → zlib压缩 → base64 编码"""
    compressed = zlib.compress(data_str.encode(DEFAULT_ENCODING))
    return base64.b64encode(compressed).decode('ascii')


def decode_decompress(b64_str):
    """base64 串 → zlib解压 → JSON 对象"""
    compressed = base64.b64decode(b64_str)
    return json.loads(zlib.decompress(compressed).decode(DEFAULT_ENCODING))


def chunk_data(data, chunk_size=DEFAULT_CHUNK_SIZE):
    """将 base64 串按固定长度分块，返回块列表"""
    return [data[i:i+chunk_size] for i in range(0, len(data), chunk_size)]


# ══════════════════════════════════════════════════
# 3. QR 载荷打包 / 解包
# ══════════════════════════════════════════════════

def create_qr_payload(index, total, chunk, filename, total_size):
    """创建单块 QR 码的 JSON 载荷字符串。"""
    payload = {
        "v": PROTOCOL_VERSION,
        "t": total,
        "i": index,
        "n": os.path.basename(filename),
        "s": total_size,
        "d": chunk,
    }
    return json.dumps(payload, separators=(',', ':'))


def parse_qr_payload(payload_str):
    """解析 QR 码载荷 JSON → dict"""
    return json.loads(payload_str)


# ══════════════════════════════════════════════════
# 4. 块组装 → 解压 → 反序列化
# ══════════════════════════════════════════════════

def assemble_chunks(chunks_dict):
    """按索引排序并拼接所有块 → 完整 base64 串"""
    indices = sorted(chunks_dict.keys())
    return ''.join(chunks_dict[i] for i in indices)


def validate_protocol(payload):
    """检查协议版本"""
    return payload.get('v') == PROTOCOL_VERSION and 'd' in payload


# ══════════════════════════════════════════════════
# 5. 文档重建（接收端）
# ══════════════════════════════════════════════════

def reconstruct_docx(blocks, output_path):
    """从内容块列表重建 docx 文档。"""
    from docx import Document
    from docx.shared import Inches, Cm, Emu
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    for block in blocks:
        btype = block.get("type", "text")
        if btype == "text":
            para = doc.add_paragraph(block.get("data", ""))
        elif btype == "heading":
            level = min(block.get("level", 1), 4)
            doc.add_heading(block.get("data", ""), level=level)
        elif btype == "image":
            try:
                img_data = base64.b64decode(block.get("data", ""))
                img_stream = BytesIO(img_data)
                doc.add_picture(img_stream, width=Inches(5.5))
            except Exception:
                doc.add_paragraph("[图片解码失败]")
        elif btype == "page_break":
            doc.add_page_break()
    doc.save(output_path)


def reconstruct_txt(blocks, output_path):
    """从内容块列表重建 txt 文件。"""
    text_parts = []
    for block in blocks:
        btype = block.get("type", "text")
        if btype == "text":
            text_parts.append(block.get("data", ""))
    with open(output_path, 'w', encoding=DEFAULT_ENCODING) as f:
        f.write('\n'.join(text_parts))


# ══════════════════════════════════════════════════
# 6. 工具函数
# ══════════════════════════════════════════════════

def _localname(elem):
    """获取 XML 元素的 localname（忽略命名空间）"""
    tag = elem.tag
    if isinstance(tag, str) and '}' in tag:
        return tag.split('}', 1)[1]
    return tag


def estimate_transfer(total_chunks, interval_seconds):
    """估算传输时间"""
    return total_chunks * interval_seconds


def format_size(size_bytes):
    """格式化文件大小"""
    for unit in ['B', 'KB', 'MB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}GB"


def format_time(seconds):
    """格式化时间"""
    if seconds < 60:
        return f"{seconds:.0f}秒"
    elif seconds < 3600:
        return f"{seconds/60:.0f}分{seconds%60:.0f}秒"
    else:
        return f"{seconds/3600:.0f}时{seconds%60/60:.0f}分"


# ══════════════════════════════════════════════════
# 8. 原始文件传输（保留完整格式）
# ══════════════════════════════════════════════════

def pack_file_data(filename, filetype, raw_bytes):
    """将文件名、类型和原始字节打包为一个 bytes。
    格式: [4字节JSON头长度][JSON头][原始文件字节]
    """
    header = json.dumps({"n": filename, "t": filetype}, separators=(',', ':')).encode('utf-8')
    return len(header).to_bytes(4, 'big') + header + raw_bytes


def unpack_file_data(packed):
    """解包 pack_file_data 的数据，返回 (filename, filetype, raw_bytes)"""
    hlen = int.from_bytes(packed[:4], 'big')
    header = json.loads(packed[4:4 + hlen])
    raw = packed[4 + hlen:]
    return header.get("n", "unknown"), header.get("t", "bin"), raw


def compress_bytes_to_b64(data):
    """压缩 bytes → zlib → base64 → str"""
    return base64.b64encode(zlib.compress(data)).decode('ascii')


def decompress_b64_to_bytes(b64_str):
    """base64 str → 尝试 zlib 解压 → bytes（自动检测是否压缩）"""
    raw = base64.b64decode(b64_str)
    try:
        return zlib.decompress(raw)
    except zlib.error:
        # 数据未压缩（如 docx/jpg 等已压缩格式直接 base64 编码）
        return raw


# 已经是压缩格式的文件类型 — zlib 再压缩基本无效
UNCOMPRESSIBLE_EXTENSIONS = {
    'docx', 'xlsx', 'pptx', 'zip', 'rar', '7z', 'gz', 'bz2', 'xz',
    'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp',
    'mp4', 'avi', 'mkv', 'mov', 'wmv', 'flv',
    'mp3', 'aac', 'wav', 'flac', 'ogg',
    'pdf',
}


def adaptive_pack(filename, filetype, raw_bytes):
    """将文件打包为传输格式，自动跳过无效压缩。

    Returns:
        (b64_payload_str, was_compressed_bool, original_size, compressed_size)
    """
    packed = pack_file_data(filename, filetype, raw_bytes)
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    data, was_compressed = try_compress(packed, ext)
    b64 = base64.b64encode(data).decode('ascii')
    return b64, was_compressed, len(packed), len(data)


def try_compress(data, file_ext=''):
    """尝试 zlib 压缩，对已知压缩格式或压缩率不足 90% 时跳过。

    Returns:
        (data_bytes, was_compressed_bool)
    """
    if file_ext.lower().lstrip('.') in UNCOMPRESSIBLE_EXTENSIONS:
        return data, False
    compressed = zlib.compress(data)
    if len(compressed) / len(data) < 0.90:
        return compressed, True
    return data, False


def adaptive_unpack(b64_str, was_compressed):
    """根据压缩标志解包：base64 → (解压) → 解包文件"""
    raw = base64.b64decode(b64_str)
    if was_compressed:
        raw = zlib.decompress(raw)
    return unpack_file_data(raw)


def optimize_docx_images(raw_bytes, max_dim=800, quality=80):
    """对 docx 中的图片进行压缩/缩放，大幅减小文件体积。

    Args:
        raw_bytes: 原始 docx 字节
        max_dim: 图片最大边长（超过则等比缩放）
        quality: JPEG 品质 1-100

    Returns:
        优化后的 docx 字节（若未改善则返回原始数据）
    """
    if raw_bytes[:2] != b'PK':
        return raw_bytes

    try:
        from PIL import Image
        from io import BytesIO
        import zipfile as zf

        input_buf = BytesIO(raw_bytes)
        output_buf = BytesIO()
        modified = False

        with zf.ZipFile(input_buf, 'r') as zin:
            with zf.ZipFile(output_buf, 'w', zf.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)

                    if item.filename.startswith('word/media/'):
                        ext = os.path.splitext(item.filename)[1].lower()
                        if ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.webp'):
                            try:
                                img = Image.open(BytesIO(data))
                                w, h = img.size
                                orig_size = len(data)

                                if max(w, h) > max_dim:
                                    ratio = max_dim / max(w, h)
                                    img = img.resize(
                                        (int(w * ratio), int(h * ratio)),
                                        Image.LANCZOS)

                                out = BytesIO()
                                if ext == '.png':
                                    if quality < 70 and img.mode in ('RGBA', 'P'):
                                        img = img.convert('RGB')
                                    if quality < 70:
                                        img.save(out, 'JPEG', quality=quality, optimize=True)
                                    else:
                                        img.save(out, 'PNG', optimize=True)
                                else:
                                    if img.mode in ('RGBA', 'P'):
                                        img = img.convert('RGB')
                                    img.save(out, 'JPEG', quality=quality, optimize=True)

                                new_data = out.getvalue()
                                if len(new_data) < orig_size * 0.9:
                                    data = new_data
                                    modified = True
                            except Exception:
                                pass

                    zout.writestr(item, data)

        if not modified:
            return raw_bytes
        optimized = output_buf.getvalue()
        if (1 - len(optimized) / len(raw_bytes)) * 100 > 3:
            return optimized
    except Exception:
        pass

    return raw_bytes


# ══════════════════════════════════════════════════
# 7. 预览函数（用于 sender 端 UI）
# ══════════════════════════════════════════════════

def preview_blocks(blocks, max_len=80):
    """生成内容预览文本"""
    lines = []
    for i, block in enumerate(blocks[:20]):
        btype = block.get("type", "?")
        data = block.get("data", "")
        if btype == "text":
            preview = data[:max_len].replace('\n', ' ')
            lines.append(f"  📝 文本: {preview}{'…' if len(data) > max_len else ''}")
        elif btype == "image":
            ext = block.get("ext", "bin")
            data_len = len(block.get("data", ""))
            raw_size = int(data_len * 0.75)  # base64 → 原始字节
            lines.append(f"  🖼️ 图片.{ext} ≈ {format_size(raw_size)}")
    if len(blocks) > 20:
        lines.append(f"  … 共 {len(blocks)} 个内容块")
    return '\n'.join(lines)
