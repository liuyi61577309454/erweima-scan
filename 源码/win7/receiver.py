"""
receiver.py - 内网接收端：摄像头扫描二维码 → 反向解析 → 重建文档

功能：
  - 实时摄像头预览 + QR码检测
  - 自动解析块数据并排序组装
  - 进度跟踪（已收/总数、缺失块标识）
  - 收齐后自动重建 docx/txt 文档
  - 可视化配置（摄像头、输出目录等）
"""

import os
import sys
import json
import time
import queue
import threading

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from io import BytesIO
from datetime import datetime

# 确保能找到同目录下的 common.py
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import common

CONFIG_PATH = os.path.join(BASE_DIR, 'receiver_config.json')

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

try:
    import numpy as np
    NP_AVAILABLE = True
except ImportError:
    NP_AVAILABLE = False

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ── MinIO 云存储 ──
try:
    from minio import Minio
    from urllib.parse import urlparse
    MINIO_AVAILABLE = True
except ImportError:
    Minio = None
    MINIO_AVAILABLE = False

# ── MySQL 数据库 ──
try:
    import pymysql
    MYSQL_AVAILABLE = True
except ImportError:
    pymysql = None
    MYSQL_AVAILABLE = False


class CameraWorker(threading.Thread):
    """摄像头工作线程：持续采集帧 → QR检测 → 放入结果队列"""

    def __init__(self, camera_id=0, frame_queue=None, result_queue=None, max_size=(640, 480)):
        super().__init__(daemon=True)
        self.camera_id = camera_id
        self.frame_queue = frame_queue or queue.Queue(maxsize=5)
        self.result_queue = result_queue or queue.Queue()
        self.max_size = max_size
        self.running = True
        self.cap = None
        self.detector = None
        self._read_failures = 0
        self._debug_frame = None  # 保存最新一帧供调试
        self._frame_lock = threading.Lock()
        self._total_frames = 0
        self._detect_attempts = 0
        self._detect_successes = 0
        self._last_frame_time = time.time()
        self._last_frame_lock = threading.Lock()
        self._camera_opened_event = threading.Event()

    def run(self):
        if not CV2_AVAILABLE:
            self.result_queue.put({"type": "error", "msg": "OpenCV 未安装，无法启动摄像头"})
            return

        try:
            self.cap = self._open_camera()
            if self.cap is None or not self.cap.isOpened():
                self.result_queue.put({"type": "error", "msg": f"无法打开摄像头 #{self.camera_id}"})
                return

            self.detector = cv2.QRCodeDetector()
            self._total_frames = 0

            while self.running:
                # 循环开始时先刷新帧时间戳，防止 cap.read() 阻塞导致 watchdog 误判
                with self._last_frame_lock:
                    self._last_frame_time = time.time()
                ret, frame = self.cap.read()
                if not ret:
                    self._read_failures += 1
                    if self._read_failures == 1:
                        self.result_queue.put({"type": "error", "msg": "摄像头读取失败，正在重试…"})
                    elif self._read_failures == 5:
                        # 轻量恢复：只重开摄像头，不重启驱动
                        self.result_queue.put({"type": "error", "msg": "尝试重新打开摄像头…"})
                        self._reopen_camera()
                    elif self._read_failures == 10:
                        # 再次轻量重开
                        self.result_queue.put({"type": "error", "msg": "再次尝试重新打开摄像头…"})
                        self._reopen_camera()
                    elif self._read_failures == 15:
                        # 多次轻量恢复失败，才重启驱动（较重量级）
                        self.result_queue.put({"type": "error", "msg": "尝试重启摄像头驱动…"})
                        self._restart_camera_driver()
                        self._reopen_camera()
                    elif self._read_failures > 35:
                        self.result_queue.put({"type": "error", "msg": "摄像头持续无响应，请停止后重试"})
                        break
                    time.sleep(0.3)
                    continue

                self._read_failures = 0
                self._total_frames += 1

                # 保存一帧的只读副本供调试（每 30 帧更新一次）
                if self._total_frames % 30 == 0:
                    with self._frame_lock:
                        self._debug_frame = frame.copy()

                try:
                    count = self._detect_qr_simple(frame)
                    self._detect_attempts += 1
                    if count > 0:
                        self._detect_successes += 1
                except Exception:
                    pass

                # 缩放帧
                h, w = frame.shape[:2]
                max_w, max_h = self.max_size
                if w > max_w or h > max_h:
                    scale = min(max_w / w, max_h / h, 1.0)
                    new_w, new_h = int(w * scale), int(h * scale)
                    frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

                if self.frame_queue.full():
                    try:
                        self.frame_queue.get_nowait()
                    except queue.Empty:
                        pass
                self.frame_queue.put(frame)

        except Exception as e:
            self.result_queue.put({"type": "error", "msg": f"摄像头异常: {e}"})
        finally:
            self._cleanup()

    def _open_camera(self):
        """仅使用 DSHOW 后端打开摄像头

        MSMF 后端已彻底移除——其 cap.read() 可能永久阻塞且 cap.release()
        无法解除阻塞，导致工作线程变成僵尸、摄像头被永久占用且无法恢复。
        DSHOW 的 cap.release() 能正常解除 cap.read() 阻塞，使线程安全退出。
        """
        for attempt in range(3):
            try:
                cap = cv2.VideoCapture(self.camera_id, cv2.CAP_DSHOW)
                if cap is not None and cap.isOpened():
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    return cap
            except Exception:
                pass
            # 重启驱动后重试
            self._restart_camera_driver()
            time.sleep(2.0)
        return None

    def _reopen_camera(self):
        """轻量级重开摄像头（不重启驱动），失败时保留 cap 不变"""
        old = self.cap
        self.cap = None
        if old:
            try:
                old.release()
            except Exception:
                pass
        try:
            cap = cv2.VideoCapture(self.camera_id, cv2.CAP_DSHOW)
            if cap is not None and cap.isOpened():
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                self.cap = cap
                self._read_failures = 0
                self.result_queue.put({"type": "debug", "msg": "摄像头已恢复"})
                return True
            if cap:
                cap.release()
        except Exception:
            pass
        self.cap = old  # 重开失败，保留旧 cap
        return False

    def _restart_camera_driver(self):
        """重启摄像头相关服务（安全修复 MF_E_INVALIDREQUEST，不触及设备禁用）"""
        try:
            import subprocess
            ps = (
                'Restart-Service -Name FrameServer -Force -ErrorAction SilentlyContinue; '
                'Restart-Service -Name CaptureService -Force -ErrorAction SilentlyContinue; '
                'Start-Sleep -Seconds 2'
            )
            subprocess.run(["powershell", "-Command", ps],
                           capture_output=True, timeout=20, shell=True)
            time.sleep(2.0)
        except Exception:
            pass

    def save_debug_frame(self, filepath):
        """保存当前调试帧到文件"""
        with self._frame_lock:
            if self._debug_frame is not None:
                cv2.imwrite(filepath, self._debug_frame)
                return True
        return False

    def get_debug_info(self):
        """返回检测统计信息"""
        return {
            "total_frames": self._total_frames,
            "detect_attempts": self._detect_attempts,
            "detect_successes": self._detect_successes,
        }

    def _detect_qr_simple(self, frame):
        """检测画面中的二维码（Win7 优化版）

        Win7 策略：
          1. pyzbar 全帧灰度检测（~50ms/帧，无缩放确保小 QR 也能识别）
          2. OpenCV 兜底（最多每 15 秒一次，缩小帧到 240px 减轻开销）

        OpenCV 的 detectAndDecode 在 Win7 上每次 1-2 秒，必须严格控制频率。
        """
        # ── 策略1：pyzbar 全帧检测 ──
        try:
            from pyzbar.pyzbar import decode as zbar_decode
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            results = zbar_decode(gray)
            if results:
                count = 0
                for res in results:
                    data = res.data.decode('utf-8')
                    if data and data.strip():
                        self.result_queue.put({"type": "qr_data", "data": data.strip()})
                        count += 1
                        pts = np.array([(p.x, p.y) for p in res.polygon],
                                       dtype=np.int32).reshape((-1, 1, 2))
                        cv2.polylines(frame, [pts], True, (0, 255, 0), 3)
                if count > 0:
                    return count
        except Exception:
            pass

        # ── 策略2：OpenCV 兜底（最多每 15 秒一次，否则 Win7 直接卡死） ──
        now = time.time()
        if now - getattr(self, '_last_slow_fb', 0) > 15.0:
            self._last_slow_fb = now
            # 缩小帧到 240px 宽，尽量减轻 OpenCV 卡顿
            h, w = frame.shape[:2]
            scale = 240.0 / w if w > 240 else 1.0
            if scale < 1.0:
                small = cv2.resize(frame, (240, int(h * scale)), interpolation=cv2.INTER_LINEAR)
            else:
                small = frame
            count = self._try_detect_multi(small, frame)
            if count > 0:
                return count
            # CLAHE
            try:
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
                enhanced = clahe.apply(gray)
                enhanced_bgr = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)
                count = self._try_detect_multi(enhanced_bgr, frame)
                if count > 0:
                    return count
            except Exception:
                pass

        return 0

    def _try_detect_multi(self, scan, draw_frame):
        """在 scan 图上循环检测最多 2 个 QR，画框到 draw_frame，返回检测数量"""
        found = []
        work = scan.copy()

        # 如果 scan 和 draw_frame 尺寸不同，坐标需要缩放（如放大策略）
        h_s, w_s = scan.shape[:2]
        h_d, w_d = draw_frame.shape[:2]
        sx = w_d / w_s if w_s > 0 else 1.0
        sy = h_d / h_s if h_s > 0 else 1.0

        for _ in range(2):
            try:
                data, points, _ = self.detector.detectAndDecode(work)
            except Exception:
                break
            if not data or not data.strip() or points is None:
                break

            data = data.strip()
            found.append(data)

            # 画框到原图（坐标从 scan 坐标系映射到 draw_frame 坐标系）
            pts_int = points[0].astype(int)
            if sx != 1.0 or sy != 1.0:
                pts_int[:, 0] = (pts_int[:, 0] * sx).astype(int)
                pts_int[:, 1] = (pts_int[:, 1] * sy).astype(int)
            cv2.polylines(draw_frame, [pts_int], True, (0, 255, 0), 3)

            # 遮掉已识别区域（在 work 副本上遮罩，小padding避免覆盖相邻码）
            try:
                x, y, pw, ph = cv2.boundingRect(pts_int)
                pad = int(max(pw, ph) * 0.05) + 5
                x1 = max(0, x - pad)
                y1 = max(0, y - pad)
                x2 = min(work.shape[1], x + pw + pad)
                y2 = min(work.shape[0], y + ph + pad)
                work[y1:y2, x1:x2] = (0, 0, 0) if len(work.shape) == 3 else 0
            except Exception:
                break

        for data in found:
            self.result_queue.put({"type": "qr_data", "data": data})

        # 输出调试日志（首次检测到/每 50 次报告一次状态）
        if found:
            self.result_queue.put({"type": "debug", "msg": f"检测到 {len(found)} 个 QR 码"})
        elif self._detect_attempts > 0 and self._detect_attempts % 100 == 0:
            self.result_queue.put({
                "type": "debug",
                "msg": f"已检测 {self._detect_attempts} 帧，未发现 QR 码 "
                       f"(总帧数: {self._total_frames})"
            })

        return len(found)

    def _cleanup(self):
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap = None
        with self._frame_lock:
            self._debug_frame = None

    def stop(self):
        self.running = False
        # 尝试从外部释放摄像头，以解除 cap.read() 可能的阻塞
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def get_last_frame_time(self):
        with self._last_frame_lock:
            return self._last_frame_time

    def is_stuck(self, timeout=5.0):
        return self.running and (time.time() - self.get_last_frame_time() > timeout)

    def switch_camera(self, camera_id):
        """切换摄像头（重启线程）"""
        self.camera_id = camera_id
        self.stop()
        time.sleep(0.3)


class QRCodeData:
    """管理接收到的 QR 块数据"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.chunks = {}          # index → data
        self.total_chunks = None
        self.filename = ""
        self.total_size = 0
        self.received_set = set()
        self.start_time = None    # 首次收到块的时间戳，用于统计耗时
        self.end_time = None      # 接收完成的时间戳，固定最终耗时

    def add_chunk(self, payload):
        """添加一个解码后的块，返回 (is_new, progress_msg)"""
        if not common.validate_protocol(payload):
            return False, "协议版本不匹配，跳过"

        idx = payload["i"]
        total = payload["t"]
        chunk_data = payload["d"]

        # 初始化元数据
        if self.total_chunks is None:
            self.total_chunks = total
            self.filename = payload.get("n", "")
            self.total_size = payload.get("s", 0)
            self.start_time = time.time()

        # 去重
        if idx in self.received_set:
            return False, f"块 {idx+1}/{total} 已存在，跳过"

        self.chunks[idx] = chunk_data
        self.received_set.add(idx)

        pct = len(self.received_set) / total * 100
        return True, f"块 {idx+1}/{total} ✅ ({pct:.0f}%)"

    def is_complete(self):
        return (self.total_chunks is not None and
                len(self.received_set) == self.total_chunks)

    def missing_indices(self):
        if self.total_chunks is None:
            return []
        return [i for i in range(self.total_chunks) if i not in self.received_set]

    def get_progress(self):
        if self.total_chunks is None:
            return 0, 0
        return len(self.received_set), self.total_chunks

    def get_duration(self):
        """返回已用时间（秒），完成则固定不再增加"""
        if self.start_time is None:
            return 0.0
        if self.end_time is not None:
            return self.end_time - self.start_time
        return time.time() - self.start_time

    def assemble(self):
        """组装所有块 → 解压 → 返回原始文件字节"""
        b64_data = common.assemble_chunks(self.chunks)
        return common.decompress_b64_to_bytes(b64_data)


class ReceiverApp:
    """内网接收端主程序"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("📥 跨网文本传输 - 内网接收端（五所研制）")
        self.root.geometry("1000x950")
        self.root.minsize(700, 800)

        # ── 关闭事件 ──
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── 数据状态 ──
        self.camera_worker = None
        self.frame_queue = queue.Queue(maxsize=2)
        self.result_queue = queue.Queue()
        self.qr_files: dict[str, QRCodeData] = {}  # filename -> QRCodeData
        self.completed_files: set = set()
        self.camera_running = False
        self._camera_start_time = 0
        self._no_qr_tip_shown = False

        # ── 输出目录 ──
        self.output_dir = os.path.expanduser("~/Documents/QR传输接收")

        # ── 云存储配置 ──
        self.minio_config = {
            "endpoint": "",
            "access_key": "",
            "secret_key": "",
            "bucket": "",
            "secure": False,
        }
        self.mysql_config = {
            "host": "",
            "port": 3306,
            "user": "",
            "password": "",
            "database": "",
            "table": "file_records",
        }
        self._db_conn = None
        self._minio_client = None
        self.cloud_storage_enabled = False

        # ── 云存储配置 StringVars（供弹窗使用） ──
        self.mi_endpoint = tk.StringVar(value=self.minio_config["endpoint"])
        self.mi_access = tk.StringVar(value=self.minio_config["access_key"])
        self.mi_secret = tk.StringVar(value=self.minio_config["secret_key"])
        self.mi_bucket = tk.StringVar(value=self.minio_config["bucket"])
        self.mi_secure = tk.BooleanVar(value=self.minio_config["secure"])
        self.my_host = tk.StringVar(value=self.mysql_config["host"])
        self.my_port = tk.IntVar(value=self.mysql_config["port"])
        self.my_user = tk.StringVar(value=self.mysql_config["user"])
        self.my_pass = tk.StringVar(value=self.mysql_config["password"])
        self.my_db = tk.StringVar(value=self.mysql_config["database"])
        self.my_table = tk.StringVar(value=self.mysql_config["table"])

        # ── 构建 UI ──
        self._build_ui()
        self._load_config()
        self._start_ui_updater()
        # 启动后延迟检测云存储连通性（UI 渲染完毕后再执行）
        self.root.after(800, self._auto_check_cloud)

    # ════════════════════════════════════════════
    # UI 构建
    # ════════════════════════════════════════════

    def _build_ui(self):
        # ── 顶部工具栏 ──
        tb = ttk.Frame(self.root)
        tb.pack(fill="x", padx=10, pady=(5, 0))
        ttk.Button(tb, text="📖 操作手册", command=self._show_manual).pack(side="right")

        # ── 摄像头控制 ──
        top_frame = ttk.LabelFrame(self.root, text="📷 摄像头控制", padding=10)
        top_frame.pack(fill="x", padx=10, pady=(10, 5))

        row1 = ttk.Frame(top_frame)
        row1.pack(fill="x")
        ttk.Label(row1, text="摄像头:").pack(side="left")
        self.cam_var = tk.IntVar(value=0)
        self.cam_combo = ttk.Combobox(row1, textvariable=self.cam_var, width=10, state="readonly")
        self.cam_combo['values'] = self._detect_cameras()
        self.cam_combo.pack(side="left", padx=5)
        self.cam_combo.bind("<<ComboboxSelected>>", lambda e: self._switch_camera())

        self.btn_camera = ttk.Button(row1, text="▶ 启动摄像头", command=self._toggle_camera)
        self.btn_camera.pack(side="left", padx=10)

        ttk.Label(row1, text="  输出目录:").pack(side="left")
        self.dir_var = tk.StringVar(value=self.output_dir)
        ttk.Entry(row1, textvariable=self.dir_var, width=25).pack(side="left", padx=5)
        ttk.Button(row1, text="浏览…", command=self._select_output_dir).pack(side="left")

        # ── 摄像头预览 ──
        video_frame = ttk.LabelFrame(self.root, text="📹 实时画面（检测到QR码时绿色框标注）", padding=5)
        video_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.video_label = ttk.Label(video_frame, anchor="center", background="#1a1a1a")
        self.video_label.pack(fill="both", expand=True)

        self._show_placeholder()

        # ── 进度面板 ──
        progress_frame = ttk.LabelFrame(self.root, text="📊 传输进度", padding=8)
        progress_frame.pack(fill="x", padx=10, pady=5)

        self.status_var = tk.StringVar(value="等待启动摄像头…")
        ttk.Label(progress_frame, textvariable=self.status_var, font=("", 11, "bold")).pack(anchor="w")

        # 多文件进度列表
        columns = ("file", "progress", "received", "time", "status")
        self.file_tree = ttk.Treeview(progress_frame, columns=columns, show="headings", height=5)
        self.file_tree.heading("file", text="文件")
        self.file_tree.heading("progress", text="进度")
        self.file_tree.heading("received", text="已收/总数")
        self.file_tree.heading("time", text="耗时(秒)")
        self.file_tree.heading("status", text="状态")
        self.file_tree.column("file", width=230, minwidth=140)
        self.file_tree.column("progress", width=120, minwidth=80)
        self.file_tree.column("received", width=100, minwidth=60)
        self.file_tree.column("time", width=80, minwidth=60)
        self.file_tree.column("status", width=80, minwidth=50)
        # 数据列居中对齐
        for col in ("file", "progress", "received", "time", "status"):
            self.file_tree.column(col, anchor="center")
        self.file_tree.pack(fill="x", pady=5)

        # ── 操作按钮 ──
        action_frame = ttk.Frame(progress_frame)
        action_frame.pack(fill="x")
        self.btn_reset = ttk.Button(action_frame, text="🔄 重置", command=self._reset_receive)
        self.btn_reset.pack(side="left", padx=5)
        self.btn_save_frame = ttk.Button(action_frame, text="📸 保存帧", command=self._save_debug_frame)
        self.btn_save_frame.pack(side="left", padx=5)
        ttk.Separator(action_frame, orient="vertical").pack(side="left", fill="y", padx=5)
        self.cloud_toggle_btn = ttk.Button(action_frame, text="☁️ 云存储: 关",
                                           command=self._toggle_cloud_storage)
        self.cloud_toggle_btn.pack(side="left", padx=2)
        ttk.Button(action_frame, text="⚙️ 配置",
                   command=self._open_cloud_config_dialog).pack(side="left", padx=2)
        self.btn_clear = ttk.Button(action_frame, text="🗑 清除日志", command=self._clear_log)
        self.btn_clear.pack(side="right", padx=5)

        # ── 日志 ──
        log_frame = ttk.LabelFrame(self.root, text="📋 接收日志", padding=5)
        log_frame.pack(fill="x", padx=10, pady=(5, 10))

        self.log_text = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        self.log_text.pack(fill="x")

    # ════════════════════════════════════════════
    # 摄像头管理
    # ════════════════════════════════════════════

    # ════════════════════════════════════════════
    # 操作手册
    # ════════════════════════════════════════════

    def _show_manual(self):
        """弹出操作手册对话框"""
        dlg = tk.Toplevel(self.root)
        dlg.title("📖 内网接收端操作手册")
        dlg.minsize(620, 520)
        dlg.transient(self.root)
        dlg.grab_set()

        main_f = ttk.Frame(dlg, padding=12)
        main_f.pack(fill="both", expand=True)

        text_w = tk.Text(main_f, wrap="word", font=("微软雅黑", 10),
                         state="normal", width=70, height=28)
        scrollbar = ttk.Scrollbar(main_f, orient="vertical", command=text_w.yview)
        text_w.configure(yscrollcommand=scrollbar.set)
        text_w.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        manual = """━━━ 内网接收端操作手册 ━━━

【软件简介】
本软件用于通过摄像头扫描外网屏幕上的二维码，
自动解析并重建文件。支持多文件并行接收、自动保存、
ZIP 解压以及云存储（MinIO + MySQL）归档。

━━━ 一、摄像头选择与启动 ━━━

1. 在「摄像头」下拉框中选择要使用的摄像头
2. 点击「启动摄像头」按钮开始预览
3. 将摄像头对准外网发送端屏幕上的二维码
4. 识别到二维码时画面中会用绿色框标注

提示：如果列表中没有摄像头，点击下拉框重新检测。

━━━ 二、输出目录设置 ━━━

接收完成的文件默认保存到「~/Documents/QR传输接收」。
可点击「浏览」按钮自定义输出目录，也可手动输入路径。

━━━ 三、实时预览与 QR 码检测 ━━━

启动摄像头后，实时画面显示在主区域：
- 绿色框：表示成功检测到二维码
- 无绿色框：摄像头在运行但未检测到二维码

检测到二维码数据后自动进入解析流程。

━━━ 四、多文件接收与进度查看 ━━━

「传输进度」面板显示每个文件的接收状态：
- 文件名、进度条、已收/总数块
- 耗时（接收完成时固定不再增加）
- 状态（接收中 / 完成）

所有文件传输完成后自动保存到输出目录。

━━━ 五、文件自动保存与 ZIP 解压 ━━━

- 每个文件接收完成后自动保存到输出目录
- 如果收到的是 ZIP 压缩包，自动解压到同名文件夹
- 日志中会显示保存路径和解压文件列表

━━━ 六、云存储配置（可选） ━━━

点击「配置」按钮可设置云存储：
1. MinIO 对象存储：上传文件并生成下载链接
2. MySQL 数据库：记录文件名和下载地址

开启「云存储」开关后，接收完成的文件自动上传归档。

━━━ 七、常见问题 ━━━

Q：摄像头启动失败？
A：① 检查摄像头是否被其他程序占用
   ② 在设备管理器中确认驱动正常
   ③ 尝试切换其他摄像头索引（0/1/2）

Q：识别不到二维码？
A：① 确保发送端正在播放二维码
   ② 调整摄像头距离（20-30cm 为宜）
   ③ 调高发送端的 QR 大小或改用 1×1 网格
   ④ 检查摄像头画面是否清晰

Q：文件接收不完整？
A：① 查看日志中缺失的块号
   ② 发送端减慢播放间隔、增加重复次数
   ③ 点击「重置」后重新接收

Q：画面卡顿严重？
A：① 确保使用 Win7 优化版（安装包 win7 目录）
   ② 降低摄像头分辨率
   ③ 关闭其他占用 CPU 的程序
"""
        text_w.insert("1.0", manual)
        text_w.configure(state="disabled")

        btn_f = ttk.Frame(main_f)
        btn_f.pack(fill="x", pady=(8, 0))
        ttk.Button(btn_f, text="关闭", command=dlg.destroy).pack(side="right", padx=5)

        dlg.update_idletasks()
        w = dlg.winfo_width()
        h = dlg.winfo_height()
        x = (dlg.winfo_screenwidth() - w) // 2
        y = (dlg.winfo_screenheight() - h) // 2
        dlg.geometry(f"+{x}+{y}")

        dlg.wait_window()

    def _open_cloud_config_dialog(self):
        """☁️ 弹出云存储配置窗口（MinIO + MySQL）"""
        dlg = tk.Toplevel(self.root)
        dlg.title("☁️ 云存储配置")
        dlg.minsize(520, 360)
        dlg.transient(self.root)
        dlg.grab_set()

        main_f = ttk.Frame(dlg, padding=10)
        main_f.pack(fill="both", expand=True)
        main_f.pack(fill="both", expand=True)

        # ── MinIO 区域 ──
        minio_f = ttk.LabelFrame(main_f, text="MinIO 配置", padding=8)
        minio_f.pack(fill="x")

        r1 = ttk.Frame(minio_f); r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="地址", width=9).pack(side="left")
        ttk.Entry(r1, textvariable=self.mi_endpoint, width=35).pack(side="left", padx=2)
        ttk.Checkbutton(r1, text="HTTPS", variable=self.mi_secure).pack(side="left", padx=2)

        r2 = ttk.Frame(minio_f); r2.pack(fill="x", pady=2)
        ttk.Label(r2, text="AccessKey", width=9).pack(side="left")
        ttk.Entry(r2, textvariable=self.mi_access, width=35).pack(side="left", padx=2)
        ttk.Label(r2, text="Bucket").pack(side="left", padx=(6,0))
        ttk.Entry(r2, textvariable=self.mi_bucket, width=14).pack(side="left", padx=2)

        r3 = ttk.Frame(minio_f); r3.pack(fill="x", pady=2)
        ttk.Label(r3, text="SecretKey", width=9).pack(side="left")
        ttk.Entry(r3, textvariable=self.mi_secret, width=35, show="*").pack(side="left", padx=2)

        # ── MySQL 区域 ──
        mysql_f = ttk.LabelFrame(main_f, text="MySQL 配置", padding=8)
        mysql_f.pack(fill="x", pady=(8, 0))

        s1 = ttk.Frame(mysql_f); s1.pack(fill="x", pady=2)
        ttk.Label(s1, text="地址", width=9).pack(side="left")
        ttk.Entry(s1, textvariable=self.my_host, width=24).pack(side="left", padx=2)
        ttk.Label(s1, text="端口").pack(side="left")
        ttk.Spinbox(s1, from_=1, to=65535, textvariable=self.my_port, width=7).pack(side="left", padx=2)

        s2 = ttk.Frame(mysql_f); s2.pack(fill="x", pady=2)
        ttk.Label(s2, text="用户名", width=9).pack(side="left")
        ttk.Entry(s2, textvariable=self.my_user, width=24).pack(side="left", padx=2)
        ttk.Label(s2, text="数据库").pack(side="left", padx=(6,0))
        ttk.Entry(s2, textvariable=self.my_db, width=14).pack(side="left", padx=2)

        s3 = ttk.Frame(mysql_f); s3.pack(fill="x", pady=2)
        ttk.Label(s3, text="密码", width=9).pack(side="left")
        ttk.Entry(s3, textvariable=self.my_pass, width=24, show="*").pack(side="left", padx=2)
        ttk.Label(s3, text="表名").pack(side="left", padx=(6,0))
        ttk.Entry(s3, textvariable=self.my_table, width=14).pack(side="left", padx=2)

        # ── 按钮行 ──
        btn_f = ttk.Frame(main_f)
        btn_f.pack(fill="x", pady=(10, 4))
        ttk.Button(btn_f, text="💾 保存并关闭",
                   command=lambda: self._save_cloud_config_dlg(dlg), width=12).pack(side="left", padx=2)
        ttk.Button(btn_f, text="🔗 测试 MinIO",
                   command=self._test_minio, width=12).pack(side="left", padx=2)
        ttk.Button(btn_f, text="🗄 测试 MySQL",
                   command=self._test_mysql, width=12).pack(side="left", padx=2)

        # 状态提示
        self._dlg_status = tk.StringVar(value="配置完成后点击「保存并关闭」")
        ttk.Label(main_f, textvariable=self._dlg_status, foreground="gray",
                  font=("", 9)).pack(anchor="w", pady=(2, 0))

        # 居中显示
        dlg.update_idletasks()
        w = dlg.winfo_width()
        h = dlg.winfo_height()
        x = (dlg.winfo_screenwidth() - w) // 2
        y = (dlg.winfo_screenheight() - h) // 2
        dlg.geometry(f"+{x}+{y}")

        dlg.wait_window()

    def _save_cloud_config_dlg(self, dlg):
        """弹窗中的保存并关闭"""
        self._save_cloud_config()
        self._dlg_status.set("✅ 已保存，配置生效")
        dlg.after(400, dlg.destroy)

    def _toggle_cloud_storage(self):
        """切换云存储功能的开启/关闭"""
        self.cloud_storage_enabled = not self.cloud_storage_enabled
        self._update_cloud_toggle_btn()
        if self.cloud_storage_enabled:
            self.log("☁️ 云存储功能已开启")
        else:
            self.log("☁️ 云存储功能已关闭")

    def _update_cloud_toggle_btn(self):
        """更新开关按钮文字"""
        txt = "☁️ 云存储: 开" if self.cloud_storage_enabled else "☁️ 云存储: 关"
        self.cloud_toggle_btn.config(text=txt)

    def _detect_cameras(self):
        """检测可用的摄像头索引（仅 DSHOW 后端）"""
        available = []
        if not CV2_AVAILABLE:
            available.append(0)
            return available
        for i in range(5):
            try:
                cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
                if cap.isOpened():
                    # 尝试读取一帧确认可用
                    for _ in range(3):
                        ret, _ = cap.read()
                        if ret:
                            available.append(i)
                            break
                    cap.release()
            except Exception:
                pass
        if not available:
            available.append(0)
        return available

    def _toggle_camera(self):
        if self.camera_running:
            self._stop_camera()
        else:
            self._start_camera()

    def _start_camera(self):
        if not CV2_AVAILABLE:
            messagebox.showerror("错误", "未安装 OpenCV (cv2)，无法启动摄像头。\n请运行: pip install opencv-python")
            return

        self.qr_files.clear()
        self.completed_files.clear()
        self._update_progress_ui()

        self.camera_worker = CameraWorker(
            camera_id=self.cam_var.get(),
            frame_queue=self.frame_queue,
            result_queue=self.result_queue,
        )
        self.camera_worker.start()
        self.camera_running = True
        self._camera_start_time = time.time()
        self._no_qr_tip_shown = False
        self.btn_camera.config(text="⏹ 停止摄像头")
        self.status_var.set("📷 正在启动摄像头…")
        self.log("📷 摄像头已启动，等待QR码…")

    def _stop_camera(self, cb=None):
        """停止摄像头（非阻塞，不等待线程退出）

        DSHOW 后端下 cap.release() 会立即解除 cap.read() 阻塞，
        无需 join 等待线程退出，daemon 线程自行清理即可。

        cb: 停止后的回调（在 UI 线程中执行），可选。
        """
        if self.camera_worker:
            self.camera_worker.stop()
            self.camera_worker = None
        self.camera_running = False
        self.btn_camera.config(text="▶ 启动摄像头")
        self._show_placeholder()
        self.status_var.set("摄像头已停止")
        self.log("📷 摄像头已停止")
        if cb:
            cb()

    def _switch_camera(self):
        was_running = self.camera_running
        if was_running:
            self._stop_camera(cb=self._do_switch_start)
        else:
            self._start_camera()

    def _do_switch_start(self):
        """_switch_camera 的回调：旧摄像头已停止，启动新摄像头"""
        self._start_camera()
        self.log(f"🔄 切换到摄像头 #{self.cam_var.get()}")

    def _restart_camera_worker(self):
        """当摄像头卡死时，强制重启工作线程（非阻塞）

        DSHOW 下 cap.release() 会立即解除 cap.read() 阻塞，
        因此无需后台等待旧线程退出，直接启动新摄像头即可。
        """
        self.log("⚠️ 摄像头疑似卡死，正在强制重启…")
        self.camera_running = False
        old_worker = self.camera_worker
        self.camera_worker = None
        self.frame_queue = queue.Queue(maxsize=2)
        if old_worker:
            old_worker.stop()
        # DSHOW 下 cap.release() 立即释放设备，直接重启即可
        self._start_camera()

    def _save_debug_frame(self):
        """保存摄像头当前帧到桌面（调试用）"""
        if not self.camera_worker:
            self.log("⚠️ 摄像头未运行")
            return
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(desktop, f"qr_debug_{ts}.png")
        if self.camera_worker.save_debug_frame(path):
            self.log(f"💾 已保存调试帧: {path}")
        else:
            self.log("⚠️ 保存失败，请稍后重试")

    # ════════════════════════════════════════════
    # UI 更新循环（主线程安全）
    # ════════════════════════════════════════════

    def _start_ui_updater(self):
        """每隔 ~50ms 检查一次队列并更新 UI"""
        self._process_queues()
        self.root.after(50, self._start_ui_updater)

    def _process_queues(self):
        """处理摄像头帧队列和结果队列"""
        # ── 处理结果队列（QR数据） ──
        has_qr = False
        try:
            while True:
                result = self.result_queue.get_nowait()
                self._handle_result(result)
                if result.get("type") == "qr_data":
                    has_qr = True
        except queue.Empty:
            pass

        # ── 无 QR 提示（摄像头开启 8 秒后仍未收到任何二维码） ──
        if (self.camera_running and not has_qr and not self._no_qr_tip_shown
                and self._camera_start_time > 0
                and time.time() - self._camera_start_time > 8):
            total_qr = sum(len(qd.received_set) for qd in self.qr_files.values()) if self.qr_files else 0
            if total_qr == 0:
                self.log("💡 未检测到二维码，请尝试：")
                self.log("   1. 确保摄像头对准外网屏幕上的二维码")
                self.log("   2. 调整摄像头焦距或距离（20-40cm 为宜）")
                self.log("   3. 调高外网发送端的「QR大小」或调低「网格」为 1×1")
                self._no_qr_tip_shown = True

        # ── 处理帧队列（视频显示） ──
        if self.camera_running:
            try:
                frame = self.frame_queue.get_nowait()
                self._display_frame(frame)
            except queue.Empty:
                pass

        # ── Watchdog: 检测摄像头卡死（超过 5 秒未收到帧 → 自动重启） ──
        if (self.camera_running and self.camera_worker
                and self.camera_worker.is_stuck(timeout=8.0)):
            self._restart_camera_worker()

    def _handle_result(self, result):
        rtype = result.get("type")
        if rtype == "qr_data":
            self._on_qr_detected(result["data"])
        elif rtype == "error":
            self.log(f"❌ {result['msg']}")
            self.status_var.set(f"错误: {result['msg']}")
        elif rtype == "debug":
            self.log(f"🔍 {result['msg']}")

    def _on_qr_detected(self, data):
        """处理检测到的 QR 码数据（按文件名分流）"""
        try:
            payload = common.parse_qr_payload(data)
        except json.JSONDecodeError:
            return

        filename = payload.get('n', 'unknown')
        if not filename:
            return

        # 按文件名分流
        if filename not in self.qr_files:
            self.qr_files[filename] = QRCodeData()
            self.log(f"📄 发现新文件: {filename}")

        qr_data = self.qr_files[filename]
        is_new, msg = qr_data.add_chunk(payload)

        if is_new:
            self._update_progress_ui()

            if qr_data.is_complete() and filename not in self.completed_files:
                self.root.after(100, lambda: self._on_file_complete(filename))

    def _on_file_complete(self, filename):
        """单个文件接收完毕，自动保存（原始字节直接写入，保留完整格式）"""
        qr_data = self.qr_files.get(filename)
        if not qr_data or filename in self.completed_files:
            return
        self.completed_files.add(filename)

        qr_data.end_time = time.time()  # 固定耗时

        self.log(f"🎉 接收完成: {filename}")
        duration = qr_data.get_duration()
        if duration > 0:
            self.log(f"⏱ 耗时: {duration:.1f} 秒")
        self.status_var.set(f"✅ {filename} 传输完成！")
        self._update_progress_ui()

        try:
            packed_bytes = qr_data.assemble()
            orig_filename, filetype, file_bytes = common.unpack_file_data(packed_bytes)

            output_dir = self.dir_var.get() or self.output_dir
            os.makedirs(output_dir, exist_ok=True)

            out = os.path.join(output_dir, orig_filename)

            with open(out, 'wb') as f:
                f.write(file_bytes)

            self.log(f"✅ 已保存: {out} ({common.format_size(len(file_bytes))})")

            # 📦 如果是 ZIP 压缩包，自动解压
            is_zip = orig_filename.lower().endswith('.zip')
            extracted_files = []
            if is_zip:
                extract_dir = os.path.join(output_dir,
                                           os.path.splitext(orig_filename)[0])
                os.makedirs(extract_dir, exist_ok=True)
                extracted_files = self._extract_zip(out, extract_dir)

            # ☁️ 云存储上传（仅开启时执行）
            if self.cloud_storage_enabled:
                if extracted_files:
                    # 压缩包：逐个上传解压后的文件
                    for rel_path, abs_path in extracted_files:
                        url = self._upload_to_minio(abs_path, rel_path)
                        if url:
                            self.log(f"🔗 MinIO 下载地址: {url}")
                            self._save_to_mysql(rel_path, url)
                else:
                    # 非压缩包：上传原文件
                    url = self._upload_to_minio(out, orig_filename)
                    if url:
                        self.log(f"🔗 MinIO 下载地址: {url}")
                        self._save_to_mysql(orig_filename, url)

        except Exception as e:
            self.log(f"❌ {filename} 重建失败: {e}")
            import traceback
            self.log(traceback.format_exc())

    # ════════════════════════════════════════════
    # 视频显示
    # ════════════════════════════════════════════

    def _show_placeholder(self):
        blank = Image.new("RGB", (640, 360), (30, 30, 30))
        self._tk_img = ImageTk.PhotoImage(blank)
        self.video_label.config(image=self._tk_img)

    def _display_frame(self, frame):
        if not PIL_AVAILABLE:
            return
        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)

            # 自适应预览尺寸
            vw = self.video_label.winfo_width() or 640
            vh = self.video_label.winfo_height() or 400
            img.thumbnail((vw, vh), Image.LANCZOS)

            self._tk_img = ImageTk.PhotoImage(img)
            self.video_label.config(image=self._tk_img)
        except Exception:
            pass

    # ════════════════════════════════════════════
    # 进度显示
    # ════════════════════════════════════════════

    def _update_progress_ui(self):
        # 清空并重建树
        for row in self.file_tree.get_children():
            self.file_tree.delete(row)

        if not self.qr_files:
            self.status_var.set("📷 等待QR码…")
            return

        total_files = len(self.qr_files)
        done = len(self.completed_files)
        self.status_var.set(f"📥 共 {total_files} 个文件，已完成 {done}")

        for fname, qd in self.qr_files.items():
            recv, total = qd.get_progress()
            if total > 0:
                pct = f"{recv/total*100:.0f}%"
                bar = "■" * int(recv / total * 20) + "□" * (20 - int(recv / total * 20))
                pct_bar = f"{bar} {pct}"
            else:
                pct_bar = "等待数据…"
            status = "✅ 完成" if fname in self.completed_files else "📥 接收中"
            duration = qd.get_duration()
            time_str = f"{duration:.1f}" if duration > 0 else "-"
            self.file_tree.insert("", "end", values=(
                fname[:40], pct_bar, f"{recv}/{total}", time_str, status))

        # 文件接近完成但还差少量块时，记录缺失块号方便调试
        for fname, qd in self.qr_files.items():
            if fname in self.completed_files:
                continue
            recv, total = qd.get_progress()
            if total > 0 and recv >= total - 3 and recv < total:
                missing = qd.missing_indices()
                self.log(f"🔴 {fname} 还缺 {len(missing)} 块: {missing[:10]}{'…' if len(missing)>10 else ''}")

    # ════════════════════════════════════════════
    # ZIP 解压
    # ════════════════════════════════════════════

    def _extract_zip(self, zip_path, extract_dir):
        """解压 ZIP 文件，返回 [(相对路径, 绝对路径), ...]"""
        import zipfile
        extracted = []
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for name in zf.namelist():
                    clean = os.path.normpath(name)
                    if clean.startswith('..') or os.path.isabs(clean):
                        self.log(f"⚠️ 跳过不安全的路径: {name}")
                        continue
                    zf.extract(name, extract_dir)
                    full_path = os.path.join(extract_dir, clean)
                    if os.path.isfile(full_path):
                        extracted.append((clean, full_path))
            self.log(f"📦 已解压 {len(extracted)} 个文件到: {extract_dir}")
            return extracted
        except Exception as e:
            self.log(f"❌ 解压失败: {e}")
            import traceback
            self.log(traceback.format_exc())
            return []

    # ════════════════════════════════════════════
    # 云存储（MinIO + MySQL）
    # ════════════════════════════════════════════

    def _update_cloud_config_from_ui(self):
        """从 UI 控件读取云存储配置到 self.minio_config / self.mysql_config"""
        self.minio_config["endpoint"] = self.mi_endpoint.get().strip()
        self.minio_config["access_key"] = self.mi_access.get().strip()
        self.minio_config["secret_key"] = self.mi_secret.get().strip()
        self.minio_config["bucket"] = self.mi_bucket.get().strip()
        self.minio_config["secure"] = self.mi_secure.get()

        self.mysql_config["host"] = self.my_host.get().strip()
        self.mysql_config["port"] = self.my_port.get()
        self.mysql_config["user"] = self.my_user.get().strip()
        self.mysql_config["password"] = self.my_pass.get().strip()
        self.mysql_config["database"] = self.my_db.get().strip()
        self.mysql_config["table"] = self.my_table.get().strip() or "file_records"

    def _save_cloud_config(self):
        """保存云存储配置到文件"""
        self._update_cloud_config_from_ui()
        try:
            cfg = {
                "cloud_storage_enabled": self.cloud_storage_enabled,
                "minio_endpoint": self.minio_config["endpoint"],
                "minio_access_key": self.minio_config["access_key"],
                "minio_secret_key": self.minio_config["secret_key"],
                "minio_bucket": self.minio_config["bucket"],
                "minio_secure": self.minio_config["secure"],
                "mysql_host": self.mysql_config["host"],
                "mysql_port": self.mysql_config["port"],
                "mysql_user": self.mysql_config["user"],
                "mysql_password": self.mysql_config["password"],
                "mysql_database": self.mysql_config["database"],
                "mysql_table": self.mysql_config["table"],
            }
            # 保留原有配置
            existing = {}
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                        existing = json.load(f)
                except Exception:
                    pass
            existing.update(cfg)
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            self.log("💾 云存储配置已保存")

            # 重新初始化客户端
            self._init_minio_client()
            self._init_database()
        except Exception as e:
            self.log(f"❌ 保存配置失败: {e}")

    def _load_config(self):
        """从文件加载云存储配置"""
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            self.cloud_storage_enabled = cfg.get("cloud_storage_enabled", False)
            self._update_cloud_toggle_btn()
            self.minio_config["endpoint"] = cfg.get("minio_endpoint", "")
            self.minio_config["access_key"] = cfg.get("minio_access_key", "")
            self.minio_config["secret_key"] = cfg.get("minio_secret_key", "")
            self.minio_config["bucket"] = cfg.get("minio_bucket", "")
            self.minio_config["secure"] = cfg.get("minio_secure", False)
            self.mysql_config["host"] = cfg.get("mysql_host", "")
            self.mysql_config["port"] = cfg.get("mysql_port", 3306)
            self.mysql_config["user"] = cfg.get("mysql_user", "")
            self.mysql_config["password"] = cfg.get("mysql_password", "")
            self.mysql_config["database"] = cfg.get("mysql_database", "")
            self.mysql_config["table"] = cfg.get("mysql_table", "file_records")
            self._sync_cloud_ui()
            self._init_minio_client()
            self._init_database()
        except Exception as e:
            self.log(f"⚠️ 加载配置: {e}")

    def _sync_cloud_ui(self):
        """将配置值同步到 UI 控件"""
        self.mi_endpoint.set(self.minio_config["endpoint"])
        self.mi_access.set(self.minio_config["access_key"])
        self.mi_secret.set(self.minio_config["secret_key"])
        self.mi_bucket.set(self.minio_config["bucket"])
        self.mi_secure.set(self.minio_config["secure"])
        self.my_host.set(self.mysql_config["host"])
        self.my_port.set(self.mysql_config["port"])
        self.my_user.set(self.mysql_config["user"])
        self.my_pass.set(self.mysql_config["password"])
        self.my_db.set(self.mysql_config["database"])
        self.my_table.set(self.mysql_config["table"])

    def _init_minio_client(self):
        """初始化 MinIO 客户端"""
        self._minio_client = None
        if not MINIO_AVAILABLE:
            return
        ep = self.minio_config["endpoint"]
        ak = self.minio_config["access_key"]
        sk = self.minio_config["secret_key"]
        if not ep or not ak or not sk:
            return
        try:
            self._minio_client = Minio(
                ep,
                access_key=ak,
                secret_key=sk,
                secure=self.minio_config["secure"],
            )
            self.log(f"🔗 MinIO 客户端已初始化: {ep}")
        except Exception as e:
            self.log(f"⚠️ MinIO 初始化失败: {e}")

    def _init_database(self):
        """初始化 MySQL 连接 + 创建表"""
        self._db_conn = None
        if not MYSQL_AVAILABLE:
            return
        host = self.mysql_config["host"]
        user = self.mysql_config["user"]
        password = self.mysql_config["password"]
        db = self.mysql_config["database"]
        table = self.mysql_config["table"]
        if not host or not user or not db or not table:
            return
        port = self.mysql_config.get("port", 3306)
        try:
            conn = pymysql.connect(
                host=host, port=port, user=user, password=password,
                database=db, charset='utf8mb4',
            )
            with conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS `{table}` (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        filename VARCHAR(255) NOT NULL COMMENT '文件名',
                        file_url VARCHAR(1024) NOT NULL COMMENT 'MinIO 下载地址',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    COMMENT='文件传输记录'
                """)
                conn.commit()
            self._db_conn = conn
            self.log(f"🗄 MySQL 已连接，表 {db}.{table} 就绪")
        except Exception as e:
            self.log(f"⚠️ MySQL 连接失败: {e}")

    def _upload_to_minio(self, local_path, filename):
        """上传文件到 MinIO，返回可公开访问的 URL（或 None）"""
        if not self._minio_client:
            self.log("⏭ MinIO 未配置，跳过上传")
            return None
        bucket = self.minio_config["bucket"]
        if not bucket:
            self.log("⏭ MinIO bucket 未配置")
            return None
        try:
            # 检查 bucket 是否存在，不存在则创建
            if not self._minio_client.bucket_exists(bucket):
                self._minio_client.make_bucket(bucket)
                self.log(f"📦 已创建 bucket: {bucket}")

            object_name = f"{int(time.time())}_{filename}"
            self._minio_client.fput_object(bucket, object_name, local_path)
            self.log(f"☁️ 已上传到 MinIO: {bucket}/{object_name}")

            # 构造可直接访问的 URL
            ep = self.minio_config["endpoint"]
            secure = self.minio_config["secure"]
            scheme = "https" if secure else "http"
            # 如果 endpoint 已经包含 scheme，解析它
            if "://" in ep:
                parsed = urlparse(ep)
                host = parsed.netloc or parsed.hostname
                scheme = parsed.scheme
            else:
                host = ep
            url = f"{scheme}://{host}/{bucket}/{object_name}"
            return url
        except Exception as e:
            self.log(f"❌ MinIO 上传失败: {e}")
            return None

    def _save_to_mysql(self, filename, file_url):
        """保存文件记录到 MySQL"""
        if not self._db_conn:
            self.log("⏭ MySQL 未连接，跳过记录")
            return
        table = self.mysql_config["table"]
        try:
            with self._db_conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO `{table}` (filename, file_url) VALUES (%s, %s)",
                    (filename, file_url)
                )
            self._db_conn.commit()
            self.log(f"🗄 已记录到数据库: {filename}")
        except Exception as e:
            self.log(f"❌ MySQL 写入失败: {e}")

    def _test_minio(self):
        """测试 MinIO 连接"""
        self._update_cloud_config_from_ui()
        if not MINIO_AVAILABLE:
            self.log("❌ 未安装 minio 库，请执行: pip install minio")
            return
        ep = self.minio_config["endpoint"]
        ak = self.minio_config["access_key"]
        sk = self.minio_config["secret_key"]
        if not ep or not ak or not sk:
            self.log("❌ 请完整填写 MinIO 配置（地址、AccessKey、SecretKey）")
            return
        try:
            client = Minio(ep, access_key=ak, secret_key=sk,
                           secure=self.minio_config["secure"])
            buckets = client.list_buckets()
            names = [b.name for b in buckets]
            self.log(f"✅ MinIO 连接成功！已有 bucket: {', '.join(names) or '(无)'}")
        except Exception as e:
            self.log(f"❌ MinIO 连接失败: {e}")

    def _test_mysql(self):
        """测试 MySQL 连接"""
        self._update_cloud_config_from_ui()
        if not MYSQL_AVAILABLE:
            self.log("❌ 未安装 pymysql 库，请执行: pip install pymysql")
            return
        host = self.mysql_config["host"]
        user = self.mysql_config["user"]
        password = self.mysql_config["password"]
        db = self.mysql_config["database"]
        if not host or not user or not db:
            self.log("❌ 请完整填写 MySQL 配置（地址、用户名、密码、数据库）")
            return
        try:
            conn = pymysql.connect(
                host=host, port=self.mysql_config.get("port", 3306),
                user=user, password=password, database=db, charset='utf8mb4',
            )
            self.log(f"✅ MySQL 连接成功！({host}:{self.mysql_config.get('port', 3306)})")
            conn.close()
        except Exception as e:
            self.log(f"❌ MySQL 连接失败: {e}")

    # ════════════════════════════════════════════
    # 启动自检：云存储连通性检测
    # ════════════════════════════════════════════

    def _auto_check_cloud(self):
        if not self.minio_config["endpoint"] or not self.mysql_config["host"]:
            return

        def check():
            ok = True
            try:
                client = Minio(
                    self.minio_config["endpoint"],
                    access_key=self.minio_config["access_key"],
                    secret_key=self.minio_config["secret_key"],
                    secure=self.minio_config["secure"],
                )
                client.list_buckets()
                self.log("✅ 启动自检：MinIO 连接正常")
            except Exception as e:
                self.log(f"⏭ 启动自检：MinIO 不可用 ({e})")
                ok = False

            if ok:
                try:
                    conn = pymysql.connect(
                        host=self.mysql_config["host"],
                        port=self.mysql_config.get("port", 3306),
                        user=self.mysql_config["user"],
                        password=self.mysql_config["password"],
                        database=self.mysql_config["database"],
                        charset='utf8mb4',
                    )
                    conn.close()
                    self.log("✅ 启动自检：MySQL 连接正常")
                except Exception as e:
                    self.log(f"⏭ 启动自检：MySQL 不可用 ({e})")
                    ok = False

            def update():
                if ok:
                    self.cloud_storage_enabled = True
                    self._update_cloud_toggle_btn()
                    self.log("☁️ 云存储已自动开启")
                else:
                    self.log("💡 云存储配置不完整或服务未就绪，未自动开启")
            self.root.after(0, update)

        threading.Thread(target=check, daemon=True).start()

    # ════════════════════════════════════════════
    # 重置 / 清理
    # ════════════════════════════════════════════

    def _reset_receive(self):
        self.qr_files.clear()
        self.completed_files.clear()
        self._update_progress_ui()
        self.status_var.set("已重置，等待新数据…")
        self.log("🔄 已重置接收状态")

    def _select_output_dir(self):
        path = filedialog.askdirectory(title="选择输出目录", initialdir=self.output_dir)
        if path:
            self.output_dir = path
            self.dir_var.set(path)

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    # ════════════════════════════════════════════
    # 日志
    # ════════════════════════════════════════════

    def log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ════════════════════════════════════════════
    # 关闭
    # ════════════════════════════════════════════

    def _on_close(self):
        """关闭窗口（不等待摄像头线程退出，daemon 线程自动随进程结束）"""
        if self.camera_worker:
            self.camera_worker.stop()
            self.camera_worker = None
        self.camera_running = False
        # 关闭数据库连接
        if self._db_conn:
            try:
                self._db_conn.close()
            except Exception:
                pass
            self._db_conn = None
        self.root.destroy()

    # ════════════════════════════════════════════
    # 启动
    # ════════════════════════════════════════════

    def run(self):
        self.log("🚀 内网接收端 v2（支持多文件并行接收）")
        self.log("💡 启动摄像头后对准外网屏幕上的二维码")
        self.log("💡 自动按文件名分流，每个文件独立组装保存")
        if not CV2_AVAILABLE:
            self.log("⚠️ 未检测到 OpenCV，请安装: pip install opencv-python")
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self._on_close()


def main():
    app = ReceiverApp()
    app.run()


if __name__ == '__main__':
    main()
