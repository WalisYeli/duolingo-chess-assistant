# -*- coding: utf-8 -*-
"""Duolingo 国际象棋自动对战 - 图形界面 (双击 start_gui.bat 启动)"""
import duolingo_chess as d  # 必须最先导入: 里面会设置 DPI 感知, 之后才能创建 Tk

import argparse
import base64
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

import chess
import cv2
import pyautogui

SETTINGS = os.path.join(d.HERE, "gui_settings.json")
UI = "Microsoft YaHei UI"
COLOR_MAP = {"自动": "auto", "白方": "white", "黑方": "black"}
TURN_MAP = {"我方": "us", "对手": "them"}
GLYPH = {"P": "♙", "N": "♘", "B": "♗", "R": "♖", "Q": "♕", "K": "♔",
         "p": "♟", "n": "♞", "b": "♝", "r": "♜", "q": "♛", "k": "♚"}


class QueueWriter:
    """把 print 输出转发到界面日志 (pythonw 下 sys.stdout 为 None, 也能正常工作)."""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(("log", s))
        return len(s)

    def flush(self):
        pass


def select_region(root):
    """全屏半透明遮罩上拖拽框选, 返回屏幕坐标 (x1, y1, x2, y2); 取消返回 None."""
    res = {}
    top = tk.Toplevel(root)
    top.attributes("-fullscreen", True)
    top.attributes("-alpha", 0.3)
    top.attributes("-topmost", True)
    top.configure(bg="black")
    cv = tk.Canvas(top, bg="black", highlightthickness=0, cursor="crosshair")
    cv.pack(fill="both", expand=True)
    cv.create_text(top.winfo_screenwidth() // 2, 70, fill="white", font=(UI, 20),
                   text="按住左键, 从棋盘左上角拖到右下角, 松开确认   (Esc 取消)")
    st = {}

    def down(e):
        st["c0"], st["p0"] = (e.x, e.y), (e.x_root, e.y_root)
        st["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=2)

    def move(e):
        if "rect" in st:
            cv.coords(st["rect"], st["c0"][0], st["c0"][1], e.x, e.y)

    def up(e):
        if "p0" in st:
            (xa, ya), (xb, yb) = st["p0"], (e.x_root, e.y_root)
            res["rect"] = (min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb))
        top.destroy()

    cv.bind("<ButtonPress-1>", down)
    cv.bind("<B1-Motion>", move)
    cv.bind("<ButtonRelease-1>", up)
    top.bind("<Escape>", lambda e: top.destroy())
    top.focus_force()
    root.wait_window(top)
    return res.get("rect")


class App:
    S = 38  # 预览棋盘每格像素

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.thread = None
        self.stop_evt = threading.Event()
        self._photo = None
        sys.stdout = sys.stderr = QueueWriter(self.q)

        root.title("多邻国国际象棋助手")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        root.report_callback_exception = self.on_cb_error
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        st = self.load_settings()
        main = ttk.Frame(root, padding=8)
        main.pack(fill="both", expand=True)

        side = self.S * 8
        self.cv = tk.Canvas(main, width=side, height=side, highlightthickness=1, highlightbackground="#888")
        self.cv.pack()
        self.status_var = tk.StringVar(value="就绪: 先 ① 校准, 再 ② 学习")
        ttk.Label(main, textvariable=self.status_var, font=(UI, 11, "bold"),
                  wraplength=side).pack(fill="x", pady=(6, 2))

        bf = ttk.Frame(main)
        bf.pack(fill="x", pady=2)
        self.setup_btns = [
            ttk.Button(bf, text="① 校准棋盘", command=self.do_calibrate),
            ttk.Button(bf, text="② 学习棋子", command=self.do_learn),
            ttk.Button(bf, text="③ 识别检查", command=self.do_check),
        ]
        for b in self.setup_btns:
            b.pack(side="left", expand=True, fill="x", padx=2)

        mf = ttk.Frame(main)
        mf.pack(fill="x", pady=(4, 0))
        self.v_mode = tk.StringVar(value=st.get("mode", "自动"))
        ttk.Label(mf, text="模式").pack(side="left", padx=(2, 6))
        for name in ("自动", "手动"):
            ttk.Radiobutton(mf, text=name + "模式", value=name, variable=self.v_mode,
                            command=self.apply_mode).pack(side="left", padx=4)

        sf = ttk.LabelFrame(main, text="手动设置", padding=6)
        self.sf = sf
        sf.pack(fill="x", pady=4)
        self.v_time = tk.StringVar(value=st.get("time", "1.0"))
        self.v_avoid = tk.BooleanVar(value=st.get("avoid_draw", True))
        self.v_threads = tk.StringVar(value=st.get("threads", str(max(1, (os.cpu_count() or 4) - 2))))
        self.v_hash = tk.StringVar(value=st.get("hash", "512"))
        self.v_color = tk.StringVar(value=st.get("color", "自动"))
        self.v_turn = tk.StringVar(value=st.get("turn", "我方"))
        self.v_click = tk.BooleanVar(value=st.get("click", False))
        self.v_learn = tk.BooleanVar(value=st.get("learn", False))
        cells = [
            ("每步思考(秒)", ttk.Spinbox(sf, from_=0.05, to=60, increment=0.1, textvariable=self.v_time, width=7)),
            ("线程数", ttk.Spinbox(sf, from_=1, to=512, textvariable=self.v_threads, width=7)),
            ("哈希(MB)", ttk.Spinbox(sf, from_=16, to=65536, increment=64, textvariable=self.v_hash, width=7)),
            ("我方颜色", ttk.Combobox(sf, values=list(COLOR_MAP), textvariable=self.v_color, width=6, state="readonly")),
            ("中途接管时轮到", ttk.Combobox(sf, values=list(TURN_MAP), textvariable=self.v_turn, width=6, state="readonly")),
            ("点击代替拖动", ttk.Checkbutton(sf, variable=self.v_click)),
            ("避免和棋", ttk.Checkbutton(sf, variable=self.v_avoid)),
            ("收录棋谱到 games/", ttk.Checkbutton(sf, variable=self.v_learn)),
        ]
        for i, (text, w) in enumerate(cells):
            r, c = divmod(i, 2)
            ttk.Label(sf, text=text).grid(row=r, column=c * 2, sticky="w", padx=(0 if c == 0 else 10, 4), pady=2)
            w.grid(row=r, column=c * 2 + 1, sticky="w", pady=2)

        rf = ttk.Frame(main)
        rf.pack(fill="x", pady=2)
        self.rf = rf
        self.btn_start = ttk.Button(rf, text="▶ 开始对战", command=self.start)
        self.btn_stop = ttk.Button(rf, text="■ 停止", command=self.stop, state="disabled")
        self.btn_start.pack(side="left", expand=True, fill="x", padx=2)
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=2)

        ttk.Label(main, text="紧急停止: 把鼠标甩到屏幕左上角", foreground="#a00").pack(anchor="w", pady=(4, 0))
        lf = ttk.Frame(main)
        lf.pack(fill="both", expand=True, pady=2)
        self.log = tk.Text(lf, height=9, width=46, font=("Consolas", 9), wrap="word")
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        self.log.config(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.render_board(lambda r, c: chess.Board().piece_at(d.sq_of(r, c, True)))
        self.apply_mode()

        # 停靠在屏幕右上角, 尽量不挡住棋盘 (可以随意拖动)
        root.update_idletasks()
        x = max(0, root.winfo_screenwidth() - root.winfo_reqwidth() - 12)
        root.geometry("+%d+%d" % (x, 20))
        self.poll()

    # ---- 设置 ----
    @staticmethod
    def load_settings():
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def is_auto(self):
        return self.v_mode.get() == "自动"

    def apply_mode(self):
        """自动模式只显示 开始/停止; 手动模式显示全部参数."""
        if self.is_auto():
            self.sf.pack_forget()
        else:
            self.sf.pack(fill="x", pady=4, before=self.rf)

    def save_settings(self):
        data = {"mode": self.v_mode.get(), "time": self.v_time.get(), "threads": self.v_threads.get(), "hash": self.v_hash.get(),
                "color": self.v_color.get(), "turn": self.v_turn.get(), "click": bool(self.v_click.get()),
                "avoid_draw": bool(self.v_avoid.get()), "learn": bool(self.v_learn.get())}
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def collect_args(self):
        if self.is_auto():
            return d.auto_args()
        t, th, hs = float(self.v_time.get()), int(self.v_threads.get()), int(self.v_hash.get())
        if not (0.02 <= t <= 60 and 1 <= th <= 512 and 16 <= hs <= 65536):
            raise ValueError("范围无效")
        return argparse.Namespace(time=t, threads=th, hash=hs,
                                  color=COLOR_MAP[self.v_color.get()], turn=TURN_MAP[self.v_turn.get()],
                                  click=bool(self.v_click.get()), avoid_draw=bool(self.v_avoid.get()), auto=False,
                                  learn=bool(self.v_learn.get()))

    # ---- 显示 ----
    def render_board(self, get_piece):
        cv, S = self.cv, self.S
        cv.delete("all")
        for r in range(8):
            for c in range(8):
                cv.create_rectangle(c * S, r * S, (c + 1) * S, (r + 1) * S, width=0,
                                    fill="#f0d9b5" if (r + c) % 2 == 0 else "#b58863")
                p = get_piece(r, c)
                if p:
                    cv.create_text(c * S + S // 2, r * S + S // 2, text=GLYPH[p.symbol()],
                                   font=("Segoe UI Symbol", int(S * 0.62)), fill="black")

    def show_image(self, img):
        s = self.S * 8
        ok, buf = cv2.imencode(".png", cv2.resize(img, (s, s), interpolation=cv2.INTER_AREA))
        self._photo = tk.PhotoImage(data=base64.b64encode(buf.tobytes()).decode("ascii"))
        self.cv.delete("all")
        self.cv.create_image(0, 0, anchor="nw", image=self._photo)

    def append_log(self, s):
        self.log.insert("end", s)
        if int(self.log.index("end-1c").split(".")[0]) > 600:
            self.log.delete("1.0", "200.0")
        self.log.see("end")

    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "log":
                    self.append_log(m[1])
                elif m[0] == "status":
                    self.status_var.set(m[1])
                elif m[0] == "board":
                    b, wb = chess.Board(m[1]), m[2]
                    self.render_board(lambda r, c: b.piece_at(d.sq_of(r, c, wb)))
                elif m[0] == "done":
                    self.set_running(False)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def on_cb_error(self, exc, val, tb):
        print("".join(traceback.format_exception(exc, val, tb)))

    def set_running(self, running):
        self.btn_start.config(state="disabled" if running else "normal")
        self.btn_stop.config(state="normal" if running else "disabled")
        for b in self.setup_btns:
            b.config(state="disabled" if running else "normal")

    # ---- 准备步骤 ----
    def need_files(self, tpl=False):
        if not os.path.exists(d.CFG):
            messagebox.showwarning("提示", "还没有校准棋盘, 请先点「① 校准棋盘」")
            return False
        if tpl and not os.path.exists(d.TPL):
            messagebox.showwarning("提示", "还没有棋子模板, 请先在初始局面点「② 学习棋子」")
            return False
        return True

    def grab_board(self):
        """先把本窗口藏起来再截图, 避免窗口入镜."""
        cfg = d.load_cfg()
        self.root.withdraw()
        self.root.update()
        time.sleep(0.25)
        try:
            return d.Screen(cfg).grab()
        finally:
            self.root.deiconify()

    def do_calibrate(self):
        cfg, img, rect, fitted = None, None, None, False
        self.root.withdraw()
        self.root.update()
        time.sleep(0.25)
        try:
            rect = select_region(self.root)
            self.root.update()
            time.sleep(0.25)  # 等遮罩消失再截图
            if rect:
                x1, y1, x2, y2 = rect
                if x2 - x1 >= 80 and y2 - y1 >= 80:
                    cfg = {"left": x1, "top": y1, "width": x2 - x1, "height": y2 - y1}
                    cfg, fitted = d.refine_region(cfg)  # 自动贴合真实的 8x8 网格
                    img = d.Screen(cfg).grab()
        finally:
            self.root.deiconify()
        if not rect:
            print("已取消校准\n")
            return
        if img is None:
            messagebox.showwarning("提示", "框选区域太小, 请重试")
            return
        w, h = cfg["width"], cfg["height"]
        if abs(w - h) > 0.03 * max(w, h):
            print("警告: 宽 %d 高 %d 不是正方形, 请确认框选的是棋盘最外沿\n" % (w, h))
        with open(d.CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        cv2.imwrite(os.path.join(d.HERE, "board_preview.png"), d.draw_grid(img))
        self.show_image(d.draw_grid(img))
        self.status_var.set("已校准, 请确认红色网格与格子对齐; 不齐就重新校准")
        print("校准完成: %s\n" % cfg)

    def do_learn(self):
        if not self.need_files():
            return
        if not messagebox.askokcancel("学习棋子", "请确认页面上是一局新对局的开局 (执黑时白方已走一步也可以), 且没有弹窗遮挡。\n继续?"):
            return
        img = self.grab_board()
        cv2.imwrite(os.path.join(d.HERE, "learn_board.png"), img)
        try:
            wb = d.learn_templates(img, COLOR_MAP[self.v_color.get()])
        except ValueError as e:
            self.status_var.set("学习失败: 当前不是开局局面")
            messagebox.showerror("学习失败", str(e))
            return
        g = d.Recognizer().grid(img)
        bad = d.opening_mismatch(d.to_map(g, wb))
        self.render_board(lambda r, c: g[r][c])
        if bad:
            self.status_var.set("警告: 初始局面有 %d 格识别不对, 检查网格是否对齐" % bad)
            print("警告: 初始局面识别误差 %d 格, 当前画面可能不是标准初始局面, 或网格没对齐\n" % bad)
        else:
            self.status_var.set("学习完成, 初始局面识别无误")
            print("自检: 初始局面识别无误\n")

    def do_check(self):
        if not self.need_files(tpl=True):
            return
        img = self.grab_board()
        rec = d.Recognizer()
        g = rec.grid(img)
        wb = d.orientation(g, COLOR_MAP[self.v_color.get()])
        self.render_board(lambda r, c: g[r][c])
        b = d.board_from_map(d.to_map(g, wb), chess.WHITE if wb else chess.BLACK)
        low = min([rec.scores[r][c] for r in range(8) for c in range(8) if g[r][c]] or [0])
        self.status_var.set("识别: %s方在下, %s" % ("白" if wb else "黑", "局面有效" if b else "局面无效, 请检查"))
        print("识别: %s方在下, 最低匹配分 %.2f\nFEN: %s\n" % ("白" if wb else "黑", low, b.fen() if b else "(无效局面)"))

    # ---- 对战 ----
    def board_covered(self):
        try:
            with open(d.CFG, encoding="utf-8") as f:
                c = json.load(f)
        except Exception:
            return False
        self.root.update_idletasks()
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        return not (rx >= c["left"] + c["width"] or rx + rw <= c["left"]
                    or ry >= c["top"] + c["height"] or ry + rh <= c["top"])

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        if not self.need_files(tpl=True):
            return
        if not os.path.exists(d.ENGINE):
            messagebox.showerror("错误", "找不到 Stockfish:\n%s" % d.ENGINE)
            return
        try:
            args = self.collect_args()
        except ValueError:
            messagebox.showwarning("提示", "设置里有无效数值:\n思考 0.02~60 秒, 线程 1~512, 哈希 16~65536 MB")
            return
        if self.board_covered() and not messagebox.askyesno(
                "提示", "本窗口挡住了棋盘区域, 会导致识别失败。\n请先把窗口拖开。仍要开始吗?"):
            return
        self.save_settings()
        self.stop_evt = threading.Event()
        self.thread = threading.Thread(target=self.worker, args=(args, self.stop_evt), daemon=True)
        self.set_running(True)
        self.status_var.set("启动引擎…")
        self.thread.start()

    def stop(self):
        self.stop_evt.set()
        self.btn_stop.config(state="disabled")
        self.status_var.set("正在停止…")

    def worker(self, args, stop_evt):
        q, bot = self.q, None
        try:
            pyautogui.PAUSE = 0
            pyautogui.FAILSAFE = True
            bot = d.Bot(args, stop_evt, lambda t: q.put(("status", t)))
            bot.on_board = lambda fen, wb: q.put(("board", fen, wb))
            print("引擎已就绪 (Threads=%d Hash=%dMB, 每步 %.2fs)\n" % (args.threads, args.hash, args.time))
            bot.run()
        except pyautogui.FailSafeException:
            print("触发 FAILSAFE (鼠标在屏幕左上角), 已中止\n")
            q.put(("status", "已中止 (FAILSAFE)"))
        except Exception:
            print(traceback.format_exc())
            q.put(("status", "出错, 详见日志"))
        finally:
            d.release_mouse()
            if bot:
                bot.close()
            q.put(("done",))

    def on_close(self):
        self.stop_evt.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        self.save_settings()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
