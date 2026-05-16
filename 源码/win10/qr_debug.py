"""
qr_debug.py - QR 摄像头检测调试工具

直接运行即可打开摄像头，实时显示 QR 检测结果。
按  S  键保存当前帧到桌面
按  Q  键退出
"""

import os, sys, time, cv2

os.environ['OPENCV_LOG_LEVEL'] = 'ERROR'


def test_one(name, cap_fn, attempts=20):
    """测试一种打开方式，返回 (cap, w, h) 或 None"""
    try:
        cap = cap_fn()
        if cap is None or not cap.isOpened():
            if cap:
                cap.release()
            return None, f"{name}: isOpened=false"
    except Exception as e:
        return None, f"{name}: {e}"

    # 多次尝试读取
    for i in range(attempts):
        ret, frame = cap.read()
        if ret and frame is not None and frame.size > 0:
            return cap, f"{name}: OK {frame.shape[1]}x{frame.shape[0]} (第{i+1}次)"
        time.sleep(0.1)

    cap.release()
    return None, f"{name}: {attempts}次read全部失败"


def main():
    print("=" * 60)
    print("QR 摄像头检测调试工具 v2")
    print("=" * 60)
    print()

    # ── 系统信息 ──
    print(f"OpenCV 版本: {cv2.__version__}")
    print(f"Python 版本: {sys.version}")
    print()

    # ── 测试多种打开策略 ──
    strategies = []

    # MSMF 系列
    for idx in [0, 1]:
        strategies.append(
            (f"MSMF index={idx} 无预热",
             lambda i=idx: cv2.VideoCapture(i)))
        strategies.append(
            (f"MSMF index={idx} 0.3s延迟",
             lambda i=idx: (time.sleep(0.3), cv2.VideoCapture(i))[1]))
        strategies.append(
            (f"MSMF index={idx} 640x480",
             lambda i=idx: (c := cv2.VideoCapture(i), c.set(cv2.CAP_PROP_FRAME_WIDTH, 640),
                            c.set(cv2.CAP_PROP_FRAME_HEIGHT, 480), c)[-1]))
        strategies.append(
            (f"MSMF index={idx} 1280x720",
             lambda i=idx: (c := cv2.VideoCapture(i), c.set(cv2.CAP_PROP_FRAME_WIDTH, 1280),
                            c.set(cv2.CAP_PROP_FRAME_HEIGHT, 720), c)[-1]))

    # DSHOW 系列
    for idx in [0, 1]:
        strategies.append(
            (f"DSHOW index={idx} 640x480",
             lambda i=idx: (c := cv2.VideoCapture(i, cv2.CAP_DSHOW),
                            c.set(cv2.CAP_PROP_FRAME_WIDTH, 640),
                            c.set(cv2.CAP_PROP_FRAME_HEIGHT, 480), c)[-1]))
        strategies.append(
            (f"DSHOW index={idx} 原生",
             lambda i=idx: cv2.VideoCapture(i, cv2.CAP_DSHOW)))

    # FFMPEG
    strategies.append(
        ("FFMPEG index=0",
         lambda: cv2.VideoCapture(0, cv2.CAP_FFMPEG)))

    # GStreamer
    strategies.append(
        ("GStreamer index=0",
         lambda: cv2.VideoCapture(0, cv2.CAP_GSTREAMER)))

    results = []
    for name, fn in strategies:
        result, msg = test_one(name, fn, attempts=15)
        status = "OK" if result else "FAIL"
        print(f"  [{status}] {msg}")
        if result:
            results.append((name, result))

    if not results:
        print("\n所有方案均失败!")
        print("请检查:")
        print("  1. Windows 设置 → 隐私和安全性 → 摄像头 → 开启摄像头访问")
        print("  2. 确认摄像头未被其他程序占用 (如 Zoom, 微信)")
        print("  3. 设备管理器 → 摄像头 → 驱动是否正常")
        input("\n按 Enter 退出…")
        return

    # ── 使用第一个成功的方式 ──
    name, cap = results[0]
    print(f"\n使用: {name}")
    print("对准二维码后按 S 保存帧, Q 退出")
    print("-" * 60)

    detector = cv2.QRCodeDetector()
    frame_count = 0
    detect_ok = 0
    detect_total = 0
    fps_start = time.time()
    fps_count = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.05)
            continue

        frame_count += 1
        fps_count += 1

        # QR 检测 (彩色原图)
        data, points, _ = detector.detectAndDecode(frame)
        if data and data.strip():
            detect_ok += 1
            print(f"  [QR] 检测到! 前50字符: {data.strip()[:50]}")
            if points is not None:
                pts = points[0].astype(int)
                cv2.polylines(frame, [pts], True, (0, 255, 0), 3)
                cv2.putText(frame, "QR OK", (pts[0][0], pts[0][1] - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        else:
            detect_total += 1
            if detect_total == 1:
                print("  摄像头画面已显示，等待识别二维码…")

        # 显示统计
        cv2.putText(frame, f"QR found: {detect_ok} | Frame: {frame_count}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        # 帧率
        if time.time() - fps_start >= 2.0:
            fps = fps_count / (time.time() - fps_start)
            if detect_ok == 0:
                print(f"  [STATS] {fps:.0f} fps, {detect_total}次检测, 无二维码")
            fps_start = time.time()
            fps_count = 0

        cv2.imshow("QR Debug - S:save Q:quit", frame)
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q') or key == 27:
            break
        elif key == ord('s'):
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(desktop, f"qr_debug_{ts}.png")
            cv2.imwrite(path, frame)
            print(f"  [SAVED] {path}")

    cap.release()
    cv2.destroyAllWindows()
    print(f"\n统计: {frame_count} 帧, {detect_ok} 次检测成功")


if __name__ == '__main__':
    main()
