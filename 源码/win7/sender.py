"""
sender.py - 外网发送端：网格二维码显示，单文件顺序发送

功能：
  - 单文件逐页显示，每页 1-4 个二维码（网格布局）
  - 文件队列，自动切换到下一文件
  - 批量目录扫描 + 自动备份
  - 图片压缩优化、QR 容量保护、配置持久化
"""

import os, sys, json, time, shutil, glob, math, zipfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import qrcode
from PIL import Image, ImageTk

if getattr(sys, 'frozen', False):
    # PyInstaller 打包后，数据文件放 exe 同级目录
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import common

CONFIG_PATH = os.path.join(BASE_DIR, 'sender_config.json')

QR_EC_MAP = {
    "L": qrcode.constants.ERROR_CORRECT_L,
    "M": qrcode.constants.ERROR_CORRECT_M,
    "Q": qrcode.constants.ERROR_CORRECT_Q,
    "H": qrcode.constants.ERROR_CORRECT_H,
}


class SenderApp:
    """外网发送端主程序"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("📤 跨网文本传输 - 外网发送端（五所研制）")
        self.root.geometry("820x950+50+50")
        self.root.minsize(700, 800)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── 配置 ──
        self.config = dict(DEFAULT_CONFIG)
        self._reload_timer = None
        self._temp_zip_paths = []       # 临时ZIP文件，退出时清理

        # ── 文件数据 ──
        self.filepath = ""
        self.filename = ""
        self.chunks = []
        self.raw_b64 = ""
        self.ctype = ""
        self.total_chunks = 0
        self.total_pages = 0
        self.current_page = 0

        # ── 播放 ──
        self.playing = False
        self.grid_size = 1
        self.loop_mode = False
        self.interval = self.config["cycle_interval"]
        self._after_id = None
        self._page_repeat = 0        # 当前页已重复次数
        self.page_repeats = 2        # 每页重复显示次数（平衡速度与可靠性）

        # ── 文件队列 ──
        self.file_queue = []
        self.current_file_idx = -1

        # ── 批量 ──
        self.scan_dir = ""
        self.backup_dir = ""
        self.batch_files = []
        self.auto_backup = True

        # ── 网格单元 ──
        self.cells = []  # list of dicts: frame, qr_label, chunk_label, _img

        self._build_ui()
        self._load_config()
        self._log_startup()

    # ════════════════════════════════════════════════════════════════
    # UI 构建
    # ════════════════════════════════════════════════════════════════

    def _build_ui(self):
        self._build_toolbar()
        self._build_batch_frame()
        self._build_queue_frame()
        self._build_player_area()
        self._build_config_frame()

        log_frame = ttk.LabelFrame(self.root, text="📋 日志", padding=5)
        log_frame.pack(fill="x", padx=10, pady=(5, 8))
        self.log_text = tk.Text(log_frame, height=4, state="disabled", wrap="word")
        self.log_text.pack(fill="x")

    def _build_toolbar(self):
        """顶部工具栏：操作手册入口"""
        tb = ttk.Frame(self.root)
        tb.pack(fill="x", padx=10, pady=(5, 0))
        ttk.Button(tb, text="📖 操作手册", command=self._show_manual).pack(side="right")

    def _show_manual(self):
        """弹出操作手册对话框"""
        dlg = tk.Toplevel(self.root)
        dlg.title("📖 外网发送端操作手册")
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

        manual = """━━━ 外网发送端操作手册 ━━━

【软件简介】
本软件用于将文件通过二维码方式传输到内网终端。
支持 docx、txt 等文档格式，以及任意文件类型的 ZIP 打包传输。

━━━ 一、文件选择与队列 ━━━

1. 「选择文件」按钮：单个选择 docx/txt 文件加入发送队列
2. 「ZIP 打包」按钮：多选任意文件打包为 ZIP 后加入队列
3. 「跳过文件」按钮：跳过当前正在发送的文件
4. 「清空队列」按钮：清空整个发送队列

文件加入队列后自动开始发送。发送完成的文件会从队列中移除。

━━━ 二、批量扫描与备份 ━━━

1. 设置「扫描目录」和「备份目录」
2. 点击「扫描」自动检测目录下的 docx/txt 文件
3. 点击「全部加入队列」一键加入所有扫描到的文件
4. 开启「自动备份」：发送完成的文件自动移至备份目录

━━━ 三、二维码网格设置 ━━━

「1×1」：每页显示 1 个二维码，码尺寸最大，识别最可靠
「1×2」：每页显示 2 个二维码，传输速度翻倍

建议：首次使用或信号不佳时选择 1×1，稳定后改用 1×2 提速。

━━━ 四、播放控制 ━━━

「播放/暂停」：开始或暂停二维码轮播
「◀ ▶」：手动翻页（上/下一页）
「循环」：开启后所有页面循环播放
「间隔」：每页停留时间（0.5-5 秒），数值越大接收端越容易识别

━━━ 五、参数配置 ━━━

「纠错级别」：L(低) / M(中) / Q(较高) / H(高)
  - L 级容量最大、二维码最密集；H 级容错最强但容量最小
  - 建议：屏幕传输用 L 级即可

「块大小」：每个二维码承载的数据量
  - 数值越大传输越快，但二维码越密集
  - 建议值：500-800

「QR 大小」：二维码的像素密度（4-12）
  - 数值越大二维码越清晰，接收端越容易识别
  - 建议值：10-12

「图片品质」：文档中图片的压缩品质（10-100%）
  - 数值越低文件越小，传输越快
  - 建议值：60-80%

「图片尺寸」：文档中图片的最大边长（200-2000px）
  - 数值越小文件越小，传输越快

━━━ 六、操作流程 ━━━

1. 启动软件，点击「选择文件」或配置批量扫描
2. 文件加载完成后自动显示二维码
3. 点击「播放」开始轮播
4. 将屏幕对准内网接收端摄像头
5. 等待所有文件发送完毕
6. 如需发送多个文件，可提前加入队列

━━━ 七、常见问题 ━━━

Q：接收端识别不到二维码？
A：① 调高 QR 大小到 10-12  ② 使用 1×1 网格
   ③ 减慢播放间隔到 1.5-2 秒  ④ 检查摄像头焦距

Q：传输速度太慢？
A：① 改用 1×2 网格  ② 调大块大小  ③ 降低图片品质和尺寸

Q：文件接收不完整？
A：① 降低纠错级别到 L  ② 减慢播放间隔
   ③ 增多页面重复次数  ④ 检查接收端日志看缺失块号
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

    def _build_batch_frame(self):
        bf = ttk.LabelFrame(self.root, text="📂 批量处理", padding=8)
        bf.pack(fill="x", padx=10, pady=(8, 2))

        r1 = ttk.Frame(bf); r1.pack(fill="x")
        ttk.Label(r1, text="扫描目录:").pack(side="left")
        self.scan_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.scan_var).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(r1, text="浏览", command=self._select_scan_dir, width=6).pack(side="right", padx=1)
        ttk.Button(r1, text="扫描", command=self._scan_directory, width=5).pack(side="right")

        r2 = ttk.Frame(bf); r2.pack(fill="x", pady=(2, 0))
        ttk.Label(r2, text="备份目录:").pack(side="left")
        self.bak_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.bak_var).pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(r2, text="浏览", command=self._select_backup_dir, width=6).pack(side="right")

        r3 = ttk.Frame(bf); r3.pack(fill="x", pady=(3, 0))
        self.btn_add_all = ttk.Button(r3, text="📥 全部加入队列", command=self._add_all_to_queue, state="disabled")
        self.btn_add_all.pack(side="left", padx=2)
        self.batch_info = tk.StringVar(value="待扫描")
        ttk.Label(r3, textvariable=self.batch_info, foreground="gray").pack(side="left", padx=10)
        self.auto_bak_cb_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r3, text="自动备份", variable=self.auto_bak_cb_var,
                        command=lambda: setattr(self, 'auto_backup', self.auto_bak_cb_var.get())
                        ).pack(side="right", padx=5)

    def _build_queue_frame(self):
        qf = ttk.LabelFrame(self.root, text="📄 文件队列", padding=6)
        qf.pack(fill="x", padx=10, pady=2)

        r = ttk.Frame(qf); r.pack(fill="x")
        self.queue_file_var = tk.StringVar(value="未选择文件")
        ttk.Label(r, textvariable=self.queue_file_var, font=("", 10, "bold")).pack(side="left")
        self.queue_info_var = tk.StringVar(value="队列: 0 个文件")
        ttk.Label(r, textvariable=self.queue_info_var, foreground="purple").pack(side="right", padx=5)

        r2 = ttk.Frame(qf); r2.pack(fill="x", pady=(2, 0))
        ttk.Button(r2, text="📁 选择文件", command=self._select_file).pack(side="left", padx=2)
        ttk.Button(r2, text="📦 ZIP打包", command=self._select_zip_pack).pack(side="left", padx=2)
        ttk.Button(r2, text="⏭ 跳过文件", command=self._skip_file).pack(side="left", padx=2)
        ttk.Button(r2, text="✖ 清空队列", command=self._clear_queue).pack(side="left", padx=10)

    def _build_player_area(self):
        pa = ttk.LabelFrame(self.root, text="📷 二维码（内网摄像头扫描）", padding=5)
        pa.pack(fill="both", expand=True, padx=10, pady=3)

        # ── 控制行 ──
        ctrl = ttk.Frame(pa)
        ctrl.pack(fill="x")

        ttk.Label(ctrl, text="网格:").pack(side="left")
        self.grid_var = tk.IntVar(value=1)
        for val, txt in [(1, "1×1"), (2, "1×2")]:
            ttk.Radiobutton(ctrl, text=txt, variable=self.grid_var, value=val,
                            command=lambda v=val: self._set_grid_size(v)
                            ).pack(side="left", padx=1)

        ttk.Separator(ctrl, orient="vertical").pack(side="left", fill="y", padx=6)

        self.btn_play = ttk.Button(ctrl, text="▶ 播放", command=self._toggle_play, width=8)
        self.btn_play.pack(side="left", padx=2)
        self.btn_prev = ttk.Button(ctrl, text="◀", command=self._prev_page, width=3)
        self.btn_prev.pack(side="left", padx=1)
        self.btn_next = ttk.Button(ctrl, text="▶", command=self._next_page, width=3)
        self.btn_next.pack(side="left", padx=1)

        ttk.Separator(ctrl, orient="vertical").pack(side="left", fill="y", padx=6)

        self.loop_cb_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="循环", variable=self.loop_cb_var,
                        command=lambda: setattr(self, 'loop_mode', self.loop_cb_var.get())
                        ).pack(side="left", padx=3)

        ttk.Label(ctrl, text="间隔:").pack(side="left", padx=(6, 0))
        self.int_var = tk.DoubleVar(value=self.config["cycle_interval"])
        ttk.Scale(ctrl, from_=0.5, to=5, variable=self.int_var, orient="horizontal",
                  length=80, command=self._on_interval_change).pack(side="left", padx=2)
        self.int_lbl = ttk.Label(ctrl, text=f"{self.config['cycle_interval']:.1f}s", width=4)
        self.int_lbl.pack(side="left")

        # ── 网格区域 ──
        self.grid_frame = ttk.Frame(pa)
        self.grid_frame.pack(fill="both", expand=True, pady=3)
        self._show_placeholder()

        # ── 进度行 ──
        prog = ttk.Frame(pa)
        prog.pack(fill="x")
        self.page_info_var = tk.StringVar(value="")
        ttk.Label(prog, textvariable=self.page_info_var, foreground="blue").pack(side="left")
        self.progress_bar = ttk.Progressbar(prog, length=200, mode="determinate")
        self.progress_bar.pack(side="right", padx=5)

    def _build_config_frame(self):
        cfg = ttk.LabelFrame(self.root, text="⚙ 配置", padding=6)
        cfg.pack(fill="x", padx=10, pady=2)

        r1 = ttk.Frame(cfg); r1.pack(fill="x")
        ttk.Label(r1, text="纠错:").pack(side="left")
        self.ec_var = tk.StringVar(value=self.config["error_correction"])
        cb = ttk.Combobox(r1, textvariable=self.ec_var, values=["L", "M", "Q", "H"], width=4, state="readonly")
        cb.pack(side="left", padx=2)
        cb.bind("<<ComboboxSelected>>", self._on_ec_change)

        ttk.Label(r1, text="块大小:").pack(side="left", padx=(8, 0))
        self.chunk_var = tk.IntVar(value=self.config["chunk_size"])
        self.chunk_spin = ttk.Spinbox(r1, from_=500, to=2200, increment=50,
                                       textvariable=self.chunk_var, width=5, command=self._on_chunk_change)
        self.chunk_spin.pack(side="left", padx=2)
        self.chunk_spin.bind("<FocusOut>", lambda e: self._on_chunk_change())
        self.chunk_spin.bind("<KeyRelease>", lambda e: self._on_chunk_change())

        ttk.Label(r1, text="QR大小:").pack(side="left", padx=(8, 0))
        self.qr_bs_var = tk.IntVar(value=self.config["qr_box_size"])
        ttk.Scale(r1, from_=4, to=12, variable=self.qr_bs_var,
                  orient="horizontal", length=60,
                  command=lambda v: self._on_qrbs_change(int(float(v)))).pack(side="left", padx=2)

        r2 = ttk.Frame(cfg); r2.pack(fill="x", pady=(2, 0))
        ttk.Label(r2, text="图片品质:").pack(side="left")
        self.iq_var = tk.IntVar(value=self.config["image_quality"])
        ttk.Scale(r2, from_=10, to=100, variable=self.iq_var, orient="horizontal",
                  length=120, command=self._on_iq_change).pack(side="left", padx=2)
        self.iq_lbl = ttk.Label(r2, text=f"{self.config['image_quality']}%", width=3)
        self.iq_lbl.pack(side="left")

        ttk.Label(r2, text="图片尺寸:").pack(side="left", padx=(8, 0))
        self.is_var = tk.IntVar(value=self.config["max_image_size"])
        self.is_spin = ttk.Spinbox(r2, from_=200, to=2000, increment=100, textvariable=self.is_var, width=5,
                    command=self._on_is_change)
        self.is_spin.pack(side="left", padx=2)
        self.is_spin.bind("<FocusOut>", lambda e: self._on_is_change())
        self.is_spin.bind("<KeyRelease>", lambda e: self._on_is_change())

    # ════════════════════════════════════════════════════════════════
    # 网格 / 占位
    # ════════════════════════════════════════════════════════════════

    def _show_placeholder(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.cells = []
        ttk.Label(
            self.grid_frame,
            text="请添加文件到队列\n点击「选择文件」或扫描目录后「全部加入队列」",
            anchor="center", foreground="gray", font=("", 12)
        ).pack(expand=True)

    def _build_grid(self):
        """按 grid_size 重建网格单元"""
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.cells = []

        n = self.grid_size

        if n == 1:
            f = ttk.Frame(self.grid_frame, relief="solid", borderwidth=1)
            f.pack(fill="both", expand=True, padx=6, pady=6)
            qr = ttk.Label(f, anchor="center", background="white")
            qr.pack(fill="both", expand=True, padx=4, pady=4)
            lbl = ttk.Label(f, text="", font=("", 10))
            lbl.pack(pady=(0, 3))
            self.cells.append({"frame": f, "qr": qr, "lbl": lbl, "_img": None})

        elif n == 2:
            row = ttk.Frame(self.grid_frame)
            row.pack(fill="both", expand=True, padx=2, pady=2)
            for i in range(2):
                f = ttk.Frame(row, relief="solid", borderwidth=1)
                f.pack(side="left", fill="both", expand=True, padx=2, pady=2)
                qr = ttk.Label(f, anchor="center", background="white")
                qr.pack(fill="both", expand=True)
                lbl = ttk.Label(f, text="", font=("", 10))
                lbl.pack(pady=(0, 3))
                self.cells.append({"frame": f, "qr": qr, "lbl": lbl, "_img": None})

        if self.total_chunks > 0:
            self.root.after(50, lambda: self._display_page(self.current_page))

    # ════════════════════════════════════════════════════════════════
    # 文件加载
    # ════════════════════════════════════════════════════════════════

    def _load_file(self, filepath):
        """加载文件 → 分块 → 显示第一页"""
        if not os.path.exists(filepath):
            self.log(f"❌ 文件不存在: {filepath}")
            return False

        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        ext = os.path.splitext(filepath)[1].lower()

        try:
            # 读取原始文件字节（完整保留格式）
            with open(filepath, 'rb') as f:
                raw_bytes = f.read()
            ext_clean = ext.lstrip('.') or 'bin'
            self.ctype = ext_clean
        except Exception as e:
            self.log(f"❌ 读取失败 {self.filename}: {e}")
            return False

        # ★ 图片压缩：对 docx 中的图片进行缩放/压缩（大幅减少体积）
        iq = self.config.get("image_quality", 80)
        ms = self.config.get("max_image_size", 800)
        raw_orig = raw_bytes
        raw_bytes = common.optimize_docx_images(raw_bytes, max_dim=ms, quality=iq)
        if len(raw_bytes) < len(raw_orig) * 0.95:
            self.log(f"🖼️ 图片已优化: {common.format_size(len(raw_orig))} → {common.format_size(len(raw_bytes))}")

        # ★ 自适应压缩：对 docx/jpg/png 等已压缩格式跳过 zlib
        self.raw_b64, self.was_compressed, orig_sz, comp_sz = \
            common.adaptive_pack(self.filename, self.ctype, raw_bytes)
        self.chunks = common.chunk_data(self.raw_b64, self.config.get("chunk_size", 500))
        self.total_chunks = len(self.chunks)
        self.total_pages = max(1, math.ceil(self.total_chunks / self.grid_size))
        self.current_page = 0

        raw_str = common.format_size(orig_sz)
        comp_str = common.format_size(comp_sz)
        flag = "🔒压缩" if self.was_compressed else "📦原始"
        self.queue_file_var.set(
            f"📄 {self.filename}  ({raw_str}→{comp_str}, {flag}, {self.total_chunks}块/{self.total_pages}页)")
        self.log(f"📄 {self.filename}: {raw_str}→{comp_str} {flag} "
                 f"({self.total_chunks}块/{self.total_pages}页)")

        self._build_grid()
        return True



    # ════════════════════════════════════════════════════════════════
    # 页面显示
    # ════════════════════════════════════════════════════════════════

    def _display_page(self, page_idx):
        """在网格中显示指定页面的二维码"""
        if not self.chunks:
            return

        page_idx = max(0, min(page_idx, self.total_pages - 1))
        self.current_page = page_idx
        start_chunk = page_idx * self.grid_size
        ec = self.config.get("error_correction", "L")

        self.grid_frame.update_idletasks()

        # 统一使用1×2的尺寸标准，1×1不放大（保持一致）
        cell_w = (self.grid_frame.winfo_width() - 10) // 2
        ds = max(240, cell_w - 5)

        for ci, cell in enumerate(self.cells):
            chunk_idx = start_chunk + ci

            if ci < self.grid_size and chunk_idx < self.total_chunks:
                payload = common.create_qr_payload(
                    chunk_idx, self.total_chunks,
                    self.chunks[chunk_idx], self.filename, len(self.raw_b64))

                try:
                    qr = qrcode.QRCode(
                        version=None,
                        error_correction=QR_EC_MAP.get(ec, qrcode.constants.ERROR_CORRECT_M),
                        box_size=self.config.get("qr_box_size", 8),
                        border=3)
                    qr.add_data(payload)
                    qr.make(fit=True)
                    img = qr.make_image(fill_color="#000000", back_color="#FFFFFF").convert("RGB")

                    img = img.resize((ds, ds), Image.NEAREST)

                    cell["_img"] = ImageTk.PhotoImage(img)
                    cell["qr"].config(image=cell["_img"])
                    cell["lbl"].config(text=f"块 {chunk_idx+1}/{self.total_chunks}", foreground="black")
                except Exception as e:
                    err_msg = str(e)
                    cell["lbl"].config(text=f"⚠ 块{chunk_idx+1} 生成失败: {err_msg[:20]}", foreground="red")
                    # 生成失败时暂停播放，防止跳过该块
                    if self.playing:
                        self._stop()
                        self.log(f"⛔ 块{chunk_idx+1} 生成失败，已暂停播放")
            else:
                # 空白占位，保持布局一致
                if cell["_img"] is None or cell["_img"].width() != ds:
                    blank = Image.new("RGB", (ds, ds), (255, 255, 255))
                    cell["_img"] = ImageTk.PhotoImage(blank)
                cell["qr"].config(image=cell["_img"])
                cell["lbl"].config(text="")

        self._update_progress()

    def _update_progress(self):
        if self.total_chunks == 0:
            self.page_info_var.set("")
            self.progress_bar["value"] = 0
            return

        start_chunk = self.current_page * self.grid_size
        end_chunk = min(start_chunk + self.grid_size - 1, self.total_chunks - 1)
        pct = (self.current_page + 1) / self.total_pages * 100
        self.page_info_var.set(
            f"页 {self.current_page + 1}/{self.total_pages}  "
            f"({start_chunk + 1}–{end_chunk + 1}/{self.total_chunks}块)")
        self.progress_bar["value"] = pct

    # ════════════════════════════════════════════════════════════════
    # 播放控制
    # ════════════════════════════════════════════════════════════════

    def _toggle_play(self):
        if self.playing:
            self._stop()
        else:
            self._play()

    def _play(self):
        if self.total_chunks == 0:
            self.log("⚠ 没有已加载的文件，请先选择文件")
            return
        self.playing = True
        self._page_repeat = 0
        self.btn_play.config(text="⏸ 暂停")
        self._after_id = self.root.after(int(self.interval * 1000), self._schedule)

    def _stop(self):
        self.playing = False
        if self._after_id:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self.btn_play.config(text="▶ 播放")

    def _schedule(self):
        if not self.playing:
            return
        self._advance_page()
        if self.playing:
            self._after_id = self.root.after(int(self.interval * 1000), self._schedule)

    def _advance_page(self):
        """定时器：前进到下一页（每页重复显示多次，确保接收端不漏）"""
        self._page_repeat += 1
        if self._page_repeat < self.page_repeats:
            # 重复显示当前页
            self._display_page(self.current_page)
            return

        self._page_repeat = 0
        next_page = self.current_page + 1
        if next_page >= self.total_pages:
            if self.loop_mode:
                self._display_page(0)
            else:
                self._stop()
                self._on_file_complete()
        else:
            self._display_page(next_page)

    def _prev_page(self):
        """手动上一页"""
        if self.playing:
            self._stop()
        if self.current_page > 0:
            self._display_page(self.current_page - 1)

    def _next_page(self):
        """手动下一页 / 末页时跳到下一文件"""
        if self.playing:
            self._stop()
        if self.current_page < self.total_pages - 1:
            self._display_page(self.current_page + 1)
        elif self.total_chunks > 0:
            self._on_file_complete()

    def _set_grid_size(self, n):
        """切换网格大小（1/2/4 每页）"""
        if self.playing:
            self._stop()
        self.grid_size = n
        self.grid_var.set(n)
        if self.total_chunks > 0:
            self.total_pages = max(1, math.ceil(self.total_chunks / n))
            self.current_page = 0
            self._build_grid()

    # ════════════════════════════════════════════════════════════════
    # 文件完成 & 队列切换
    # ════════════════════════════════════════════════════════════════

    def _on_file_complete(self):
        """当前文件所有页面展示完毕"""
        if not self.filepath:
            return
        self.log(f"✅ 完成: {self.filename}")

        # 仅备份来自扫描目录的文件
        scan_dir = os.path.normpath(os.path.abspath(self.scan_dir)) if self.scan_dir and os.path.isdir(self.scan_dir) else ""
        file_dir = os.path.normpath(os.path.dirname(os.path.abspath(self.filepath)))
        if (self.auto_backup and self.backup_dir and scan_dir
                and file_dir == scan_dir and os.path.exists(self.filepath)):
            self._backup_file(self.filepath)

        self._load_next_file()

    def _load_next_file(self):
        """从队列中移除当前文件，加载下一个并自动播放"""
        # ── 移除已完成文件 ──
        if self.current_file_idx >= 0 and self.filepath:
            removed_path = self.filepath
            if self.current_file_idx < len(self.file_queue):
                self.file_queue.pop(self.current_file_idx)
            # 如果是临时ZIP包，清理文件
            self._cleanup_completed_zip(removed_path)
            self._update_queue_ui()

        # ── 加载下一个文件（pop 后索引自动指向原下一项） ──
        while self.current_file_idx < len(self.file_queue):
            path = self.file_queue[self.current_file_idx]
            if os.path.exists(path):
                if self._load_file(path):
                    self._play()
                return
            # 文件不存在，跳过
            self.file_queue.pop(self.current_file_idx)

        # 队列空了
        self.current_file_idx = -1
        self.filepath = ""
        self.filename = ""
        self.chunks = []
        self.total_chunks = 0
        self.total_pages = 0
        self.queue_file_var.set("未选择文件")
        self._update_queue_ui()
        self._show_placeholder()
        self.log("🏁 所有文件处理完毕")

    def _cleanup_completed_zip(self, path):
        """如果完成的文件是临时ZIP包，删除它"""
        if path in self._temp_zip_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
                self._temp_zip_paths.remove(path)
            except Exception:
                pass

    # ════════════════════════════════════════════════════════════════
    # 队列管理
    # ════════════════════════════════════════════════════════════════

    def _select_file(self):
        path = filedialog.askopenfilename(
            title="选择文档",
            filetypes=[("文档文件", "*.docx *.txt"), ("Word", "*.docx"), ("文本", "*.txt"), ("所有", "*.*")])
        if path:
            self._add_to_queue(path)

    def _select_zip_pack(self):
        """多选文件 → 打包为ZIP → 加入队列"""
        paths = filedialog.askopenfilenames(
            title="选择要打包的文件（可多选）",
            filetypes=[("所有文件", "*.*")])
        if not paths:
            return

        # 生成ZIP文件名（时间戳）
        ts = time.strftime("%Y%m%d_%H%M%S")
        zip_name = f"打包_{ts}.zip"
        temp_dir = os.path.join(BASE_DIR, "temp_zip")
        os.makedirs(temp_dir, exist_ok=True)
        zip_path = os.path.join(temp_dir, zip_name)

        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for path in paths:
                    arcname = os.path.basename(path)
                    zf.write(path, arcname)
            self._temp_zip_paths.append(zip_path)
            size_str = common.format_size(os.path.getsize(zip_path))
            self.log(f"📦 已打包 {len(paths)} 个文件 → {zip_name} ({size_str})")
            self._add_to_queue(zip_path)
        except Exception as e:
            self.log(f"❌ ZIP打包失败: {e}")

    def _add_to_queue(self, filepath):
        for p in self.file_queue:
            if os.path.abspath(p) == os.path.abspath(filepath):
                self.log(f"ℹ️ 已在队列: {os.path.basename(filepath)}")
                return
        self.file_queue.append(filepath)
        self._update_queue_ui()
        self.log(f"📄 加入队列: {os.path.basename(filepath)}")
        if self.current_file_idx == -1:
            self.current_file_idx = 0
            self._load_file(self.file_queue[0])

    def _add_all_to_queue(self):
        if not self.batch_files:
            return
        count = 0
        for path in self.batch_files:
            if os.path.exists(path):
                dup = any(os.path.abspath(p) == os.path.abspath(path) for p in self.file_queue)
                if not dup:
                    self.file_queue.append(path)
                    count += 1
        self._update_queue_ui()
        self.log(f"📥 已加入 {count} 个文件到队列")
        if self.current_file_idx == -1 and self.file_queue:
            self.current_file_idx = 0
            self._load_file(self.file_queue[0])

    def _skip_file(self):
        if self.current_file_idx < 0 or not self.file_queue:
            return
        self._stop()
        self.log(f"⏭ 跳过: {self.filename}")
        self._on_file_complete()

    def _clear_queue(self):
        self._stop()
        self.file_queue.clear()
        self.current_file_idx = -1
        self.filepath = ""
        self.filename = ""
        self.chunks = []
        self.total_chunks = 0
        self.total_pages = 0
        self.queue_file_var.set("未选择文件")
        self._update_queue_ui()
        self._show_placeholder()
        self.log("🗑 队列已清空")

    def _update_queue_ui(self):
        if not self.file_queue:
            self.queue_info_var.set("队列: 0 个文件")
        elif self.current_file_idx >= 0:
            rem = len(self.file_queue) - self.current_file_idx - 1
            self.queue_info_var.set(f"队列: {rem} 个待处理 / 共 {len(self.file_queue)}")
        else:
            self.queue_info_var.set(f"队列: {len(self.file_queue)} 个文件")

    # ════════════════════════════════════════════════════════════════
    # 批量处理
    # ════════════════════════════════════════════════════════════════

    def _select_scan_dir(self):
        d = filedialog.askdirectory(title="选择扫描目录",
                                    initialdir=self.scan_dir or os.path.expanduser("~"))
        if d:
            self.scan_dir = d
            self.scan_var.set(d)
            self._scan_directory()
            self._save_config()

    def _select_backup_dir(self):
        d = filedialog.askdirectory(title="选择备份目录",
                                    initialdir=self.backup_dir or self.scan_dir or os.path.expanduser("~"))
        if d:
            self.backup_dir = d
            self.bak_var.set(d)
            self._save_config()

    def _scan_directory(self):
        d = self.scan_var.get() or self.scan_dir
        if not d or not os.path.isdir(d):
            self.log("⚠️ 无效扫描目录")
            return
        self.scan_dir = d
        seen = set()
        self.batch_files = []
        for ext in ('*.docx', '*.txt'):
            for p in glob.glob(os.path.join(d, ext)):
                if p.lower() not in seen:
                    seen.add(p.lower())
                    self.batch_files.append(p)
        self.batch_files.sort()
        if not self.batch_files:
            self.batch_info.set("未找到文件")
            self.btn_add_all.config(state="disabled")
            return
        self.batch_info.set(f"📄 {len(self.batch_files)} 个文件")
        self.btn_add_all.config(state="normal")
        self.log(f"🔍 扫描到 {len(self.batch_files)} 个文件")
        if not self.backup_dir and not self.bak_var.get():
            self.backup_dir = os.path.join(os.path.dirname(d), os.path.basename(d) + "_已处理")
            self.bak_var.set(self.backup_dir)
            os.makedirs(self.backup_dir, exist_ok=True)

    def _backup_file(self, filepath):
        if not self.backup_dir or not os.path.exists(filepath):
            return
        try:
            os.makedirs(self.backup_dir, exist_ok=True)
            dest = os.path.join(self.backup_dir, os.path.basename(filepath))
            if os.path.exists(dest):
                base, ext = os.path.splitext(dest)
                dest = f"{base}_{int(time.time())}{ext}"
            shutil.move(filepath, dest)
            self.log(f"📦 已备份: {os.path.basename(filepath)}")
        except Exception as e:
            self.log(f"⚠️ 备份失败 {os.path.basename(filepath)}: {e}")

    # ════════════════════════════════════════════════════════════════
    # 配置
    # ════════════════════════════════════════════════════════════════

    def _on_interval_change(self, val):
        v = max(0.5, float(val))
        self.int_lbl.config(text=f"{v:.1f}s")
        self.interval = v
        self.config["cycle_interval"] = v
        self._save_config()

    def _on_iq_change(self, val):
        q = int(float(val))
        self.iq_lbl.config(text=f"{q}%")
        self._schedule_update("image_quality", q)

    def _on_is_change(self):
        self._schedule_update("max_image_size", self.is_var.get())

    def _on_chunk_change(self):
        self._schedule_update("chunk_size", self.chunk_var.get())

    def _on_ec_change(self, event):
        ec = self.ec_var.get()
        safe = common.get_safe_chunk_size(ec)
        self.chunk_spin.config(to=safe)
        if self.chunk_var.get() > safe:
            self.chunk_var.set(safe)
        self._schedule_update("error_correction", ec)

    def _on_qrbs_change(self, val):
        self.config["qr_box_size"] = max(4, min(12, val))
        if self.total_chunks > 0:
            self._display_page(self.current_page)

    def _schedule_update(self, key, value):
        self.config[key] = value
        if key in ("cycle_interval", "qr_box_size"):
            self._save_config()
            return
        if self._reload_timer:
            self.root.after_cancel(self._reload_timer)
        self._reload_timer = self.root.after(600, self._do_reload)

    def _do_reload(self):
        self._reload_timer = None
        self._save_config()
        if self.filepath and os.path.exists(self.filepath):
            was_playing = self.playing
            self._stop()
            self._load_file(self.filepath)
            if was_playing:
                self._play()
        self.log("⚙ 配置更新，已重新加载")

    # ════════════════════════════════════════════════════════════════
    # 配置持久化
    # ════════════════════════════════════════════════════════════════

    def _save_config(self):
        try:
            cfg = {k: self.config[k] for k in (
                "chunk_size", "cycle_interval", "qr_box_size", "error_correction",
                "image_quality", "max_image_size")}
            cfg["scan_dir"] = self.scan_dir
            cfg["backup_dir"] = self.backup_dir
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_config(self):
        try:
            if not os.path.exists(CONFIG_PATH):
                return
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            for k in ("chunk_size", "cycle_interval", "qr_box_size", "error_correction",
                      "image_quality", "max_image_size"):
                if k in cfg:
                    self.config[k] = cfg[k]
            self.scan_dir = cfg.get("scan_dir", "")
            self.backup_dir = cfg.get("backup_dir", "")
            self.ec_var.set(self.config["error_correction"])
            self.chunk_var.set(self.config["chunk_size"])
            self.int_var.set(self.config["cycle_interval"])
            self.iq_var.set(self.config["image_quality"])
            self.is_var.set(self.config["max_image_size"])
            self.qr_bs_var.set(self.config["qr_box_size"])
            self.scan_var.set(self.scan_dir)
            self.bak_var.set(self.backup_dir)
            safe = common.get_safe_chunk_size(self.config["error_correction"])
            self.chunk_spin.config(to=safe)
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════════
    # 日志 / 生命周期
    # ════════════════════════════════════════════════════════════════

    def log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _log_startup(self):
        self.log(f"🚀 外网发送端 v{common.PROTOCOL_VERSION}（网格布局）")
        self.log("💡 选择文件或扫描目录，文件逐页显示 1-4 个二维码")
        self.log("💡 接收端摄像头同时识别多个二维码，提升传输速度")

    def _on_close(self):
        self._stop()
        self._save_config()
        self._cleanup_temp_zips()
        self.root.destroy()

    def _cleanup_temp_zips(self):
        """清理临时ZIP文件"""
        for p in self._temp_zip_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        self._temp_zip_paths.clear()
        temp_dir = os.path.join(BASE_DIR, "temp_zip")
        try:
            if os.path.isdir(temp_dir) and not os.listdir(temp_dir):
                os.rmdir(temp_dir)
        except Exception:
            pass

    def run(self):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self._on_close()


DEFAULT_CONFIG = {
    "chunk_size": 500,
    "cycle_interval": 0.8,
    "qr_box_size": 10,
    "error_correction": "L",
    "image_quality": 80,
    "max_image_size": 800,
    "scan_dir": "",
    "backup_dir": "",
    "auto_backup": True,
}


def main():
    SenderApp().run()


if __name__ == '__main__':
    main()
