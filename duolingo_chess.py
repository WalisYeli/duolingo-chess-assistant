# -*- coding: utf-8 -*-
"""Duolingo 国际象棋自动对战  (https://www.duolingo.cn/chess-matches)

用法 (先打开对局页面, 棋盘在初始局面):
    python duolingo_chess.py calibrate        # 1. 框选棋盘 (鼠标指左上角/右下角)
    python duolingo_chess.py learn            # 2. 在初始局面学习棋子模板
    python duolingo_chess.py check            # 3. 验证识别结果 (可选, 随时可用)
    python duolingo_chess.py play             # 4. 自动对战
把鼠标甩到屏幕左上角可立即中止 (pyautogui FAILSAFE), 或在终端按 Ctrl+C。
"""
import ctypes

try:  # 让截图坐标与鼠标坐标都使用物理像素, 避免 Windows 缩放导致错位
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import argparse
import json
import os
import random
import re
import threading
import time

import chess
import chess.engine
import chess.pgn
import cv2
import mss
import numpy as np
import pyautogui

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "engine", "stockfish", "stockfish-windows-x86-64-universal.exe")
CFG = os.path.join(HERE, "board.json")
TPL = os.path.join(HERE, "templates.npz")
GAMES = os.path.join(HERE, "games")  # 每局棋谱 (PGN) 保存目录

CELL = 64        # 格子归一化边长
FEAT = 32        # 匹配特征边长
SHIFTS = sorted(((dy, dx) for dy in range(-12, 13, 2) for dx in range(-6, 7, 2)),
                key=lambda t: abs(t[0]) + abs(t[1]))  # 由近到远, 近处命中足够好就提前结束
GOOD_SCORE = 0.93
PIECE_VAL = {chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
SIMPLIFY_CP = 2.0  # 均势时, 每换掉 1 点子力价值 (后=9, 车=5, 马/象=3) 扣的评分(厘兵)
MIN_SCORE = 0.55  # 模板匹配最低相关系数
BAD_CP = -40       # 最优步评分低于此值 (厘兵) 视为劣势, 直接走最优步, 和棋是好结果 (执黑时 -10~-40 仍属均势)
SIMPLIFY_MAX = 30  # 评分不超过此值时, 不主动换子简化 (领先时换子是好事, 不再限制)
TRADE_COST = 12    # 每换掉一个非兵子扣的评分 (只用于在评分接近的候选间排序)
SIMPLIFY_WINDOW = 10  # 防简化最多让出的评分 (厘兵); 防和的窗口仍是 30
MULTIPV_N = 4      # 均势时的候选步数
DRAW_WINDOW = 12   # 防和/防简化时, 单步最多让出的评分 (厘兵; 优势 > 150 时另算)
DRAW_BUDGET = 40   # 每局累计最多让出的评分 (厘兵), 用完后直接走最优步
SIMPLIFY_MIN = -15  # 评分低于此值时不罚换子 (略劣时换子往往对我方有利)
MIN_DEPTH = 18     # 打算偏离最优步时, 搜索深度不足就先加深再决定
LEARN_LOSS = 30    # 从历史棋谱学习: 对手某步估算损失 >= 此值, 就记下通向它的我方着法
FLAT_MOVES = 6     # 连续这么多步最优评分都接近 0.00 (死和局), 之后每步只搜 FLAT_TIME 秒
FLAT_TIME = 0.2
FLAT_CP = 3        # 最优评分绝对值不超过它才算"平"
LOSS_BIG = 20     # 统计对手"明显失误"的阈值 (厘兵); 估算噪声约 ±10, 小于它的差异基本是噪声
# 小开局库 (SAN): 引导进入不对称、有战斗的变例, 避免 1...e5 之类很快换成对称均势.
# 只记录我方颜色那一侧的着法; 对手偏离书本后交给引擎.
BOOK_BLACK = [   # 我方执黑
    "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6",         # 西西里
    "e4 c5 Nf3 Nc6 d4 cxd4 Nxd4 Nf6 Nc3 d6",
    "e4 c5 Nc3 Nc6 g3 g6 Bg2 Bg7",
    "e4 c5 Nf3 Nc6 Nc3 g6 d4 cxd4 Nxd4 Bg7 Be3 Nf6",   # 对手 3.Nc3: 加速龙, 避免 3...e5 的对称局面
    "e4 c5 Nf3 Nc6 Nc3 g6 Bb5 Bg7 O-O Nf6",
    "d4 Nf6 c4 g6 Nc3 Bg7 e4 d6 Nf3 O-O",           # 印度防御
    "d4 Nf6 c4 e6 Nc3 Bb4 e3 O-O",
    "c4 e5 Nc3 Nf6 Nf3 Nc6",
    "Nf3 Nf6 c4 g6 Nc3 Bg7 e4 d6",
]
BOOK_WHITE = [   # 我方执白
    "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6 Be3 e5 Nb3 Be6 f3",
    "e4 c5 Nf3 Nc6 d4 cxd4 Nxd4 Nf6 Nc3 d6 Bg5 e6 Qd2",
    "e4 e5 Nf3 Nc6 d4 exd4 Nxd4 Nf6 Nxc6 bxc6 e5 Qe7 Qe2 Nd5 c4",   # 苏格兰
    "e4 e6 d4 d5 Nc3 Nf6 Bg5 Be7 e5 Nfd7",
    "e4 c6 d4 d5 Nc3 dxe4 Nxe4 Bf5 Ng3 Bg6 h4 h6 Nf3 Nd7",
]


def _build_book(lines, color):
    """局面 (epd) -> {着法 (UCI): 权重}, 书中该局面下 color 方的候选着法. 按局面匹配, 兼容换序和中途接管."""
    book = {}
    for line in lines:
        b = chess.Board()
        for san in line.split():
            mv = b.parse_san(san)
            if b.turn == color:
                w = book.setdefault(b.epd(), {})
                w[mv.uci()] = w.get(mv.uci(), 0) + 1
            b.push(mv)
    return book


BOOK = {chess.WHITE: _build_book(BOOK_WHITE, chess.WHITE), chess.BLACK: _build_book(BOOK_BLACK, chess.BLACK)}

# 手动模式只用这个简单默认开局库 (不含自动收录的对手库)
BOOK_SIMPLE = {
    chess.WHITE: _build_book(["e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6 Be3 e5 Nb3 Be6 f3"], chess.WHITE),
    chess.BLACK: _build_book(["e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6",
                              "d4 Nf6 c4 g6 Nc3 Bg7 e4 d6 Nf3 O-O"], chess.BLACK),
}


def auto_args():
    """自动模式的全部参数: 颜色/线程/哈希自动, 启用智能思考 (防和预算/加深/死和局省时) 和自动收录获胜棋谱."""
    return argparse.Namespace(time=1.0, threads=max(1, (os.cpu_count() or 4) - 2), hash=512,
                              color="auto", turn="us", click=False, avoid_draw=True, auto=True)


def game_end(board):
    """棋局是否已结束: 棋规结束, 或网站会直接判和的情形 (第三次重复 / 50 步). 返回 Outcome 或 None."""
    o = board.outcome()
    if o is None and board.is_repetition(3):
        o = chess.Outcome(chess.Termination.THREEFOLD_REPETITION, None)
    if o is None and board.halfmove_clock >= 100:
        o = chess.Outcome(chess.Termination.FIFTY_MOVES, None)
    return o


def load_learned(color):
    """读 games/ 里的历史棋谱: 对手 (似乎是确定性的) 若在某步出现明显失误 (loss >= LEARN_LOSS),
    就把我方走到那一步之前的全部着法记成 局面(epd) -> {着法: 胜局数}, 下一局优先走胜局数最多的着法.
    只学我方赢了的棋谱 (和棋/未完成的不算), 且每局只取第一次明显失误. 返回 (book, 用到的棋谱数)."""
    book, used = {}, 0
    try:
        names = sorted(os.listdir(GAMES))
    except OSError:
        return book, used
    for name in names:
        if not name.endswith(".pgn"):
            continue
        try:
            with open(os.path.join(GAMES, name), encoding="utf-8") as f:
                game = chess.pgn.read_game(f)
            if game is None or (game.headers.get("White") == "Bot") != (color == chess.WHITE):
                continue
            if game.headers.get("Result") != ("1-0" if color == chess.WHITE else "0-1"):
                continue
            board, mine = game.board(), []
            for node in game.mainline():
                if board.turn == color:
                    mine.append((board.epd(), node.move.uci()))
                else:
                    m = re.match(r"loss~(-?\d+)", node.comment or "")
                    if m and int(m.group(1)) >= LEARN_LOSS:
                        for epd, u in set(mine):
                            w = book.setdefault(epd, {})
                            w[u] = w.get(u, 0) + 1
                        used += 1
                        break
                board.push(node.move)
        except Exception:
            continue  # 坏文件不影响对局
    return book, used


def fmt_score(cp, pawns=False):
    """评分 -> 文本. 杀棋分显示成 +M3 / -M1; pawns=True 时以兵为单位 (+0.12), 否则厘兵 (+12)."""
    if cp is None:
        return "book"
    if abs(cp) >= 90000:
        n = max(1, (100000 - abs(cp) + 1) // 2)  # 杀棋分 = 100000 - 半步数
        return "%sM%d" % ("+" if cp > 0 else "-", n)
    return "%+.2f" % (cp / 100.0) if pawns else "%+d" % cp


START_MAP = chess.Board().piece_map()


# --------------------------------------------------------------------------
# 坐标 / 棋子编码
# --------------------------------------------------------------------------
def sq_of(r, c, wb):
    """图像格 (行r, 列c) -> 棋盘格. wb: 白方在下."""
    return chess.square(c if wb else 7 - c, 7 - r if wb else r)


def rc_of(sq, wb):
    f, rk = chess.square_file(sq), chess.square_rank(sq)
    return (7 - rk, f) if wb else (rk, 7 - f)


def pidx(p):
    return p.piece_type - 1 + (0 if p.color else 6)


def pdec(i):
    return chess.Piece(i % 6 + 1, i < 6)


# --------------------------------------------------------------------------
# 截图与图像特征
# --------------------------------------------------------------------------
class Screen:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sct = mss.mss()
        self.region = {k: int(cfg[k]) for k in ("left", "top", "width", "height")}

    def grab(self):
        return np.ascontiguousarray(np.asarray(self.sct.grab(self.region))[:, :, :3])


def split(img):
    """棋盘图 -> 8x8 个 CELL x CELL 的 BGR 小图 (行优先, 图像坐标)."""
    h, w = img.shape[:2]
    ys = [round(i * h / 8) for i in range(9)]
    xs = [round(i * w / 8) for i in range(9)]
    return [[cv2.resize(img[ys[r]:ys[r + 1], xs[c]:xs[c + 1]], (CELL, CELL),
                        interpolation=cv2.INTER_AREA) for c in range(8)] for r in range(8)]


def gray_feat(cell):
    g = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY)
    m = 4
    g = g[m:CELL - m, m:CELL - m]
    return cv2.resize(g, (FEAT, FEAT), interpolation=cv2.INTER_AREA).astype(np.float32)


def unit(g):
    v = g.ravel() - g.mean()
    return v / (np.linalg.norm(v) + 1e-6)


def center_gray(cell):
    g = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY)
    return g[CELL // 5:CELL * 4 // 5, CELL // 5:CELL * 4 // 5]


def center_std(cell):
    return float(center_gray(cell).std())


OCC_STD = 6.0  # 格子中心灰度标准差高于此值视为有棋子 (空格是纯色, 接近 0)


# --------------------------------------------------------------------------
# 网格自动贴合: 框选时难免带上边框/阴影, 用方格交界线把 8x8 网格对齐
# --------------------------------------------------------------------------
def edge_profile(img, axis):
    """方格交界处的边缘强度曲线. axis=1: 沿 x (竖线), axis=0: 沿 y (横线).
    只统计弱/中等强度的边 (方格色差、边框), 忽略棋子的强轮廓."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.int16)
    d = np.abs(np.diff(g, axis=axis))
    w = np.where((d >= 4) & (d <= 110), np.minimum(d, 30), 0).astype(np.float32)
    p = np.zeros(g.shape[axis], np.float32)
    p[1:] = w.sum(axis=1 - axis)
    return np.convolve(p, np.ones(3, np.float32) / 3, mode="same")


def fit_axis(prof):
    """找 9 条等间距线 (start + k*step) 使总边缘强度最大. 返回 (start, step, 质量)."""
    n = len(prof)
    xs = np.arange(n, dtype=np.float32)
    k = np.arange(9, dtype=np.float32)

    def search(steps, lo, hi, dstart):
        best = (-1.0, 0.0, 0.0)
        for st in steps:
            starts = np.arange(lo, min(hi, n - 8 * st) + 1e-6, dstart)
            if len(starts) == 0:
                continue
            val = np.interp(starts[:, None] + st * k[None, :], xs, prof).sum(1)
            i = int(val.argmax())
            if val[i] > best[0]:
                best = (float(val[i]), float(starts[i]), float(st))
        return best

    base = n / 8.0
    v, s0, st = search(np.arange(base * 0.7, base * 1.0005, 0.1), 0, n, 0.5)
    if v < 0:
        return None
    v, s0, st = search(np.arange(st - 0.1, st + 0.1001, 0.02), s0 - 0.5, s0 + 0.5, 0.1)
    return s0, st, (v / 9) / (float(prof.mean()) + 1e-6)


def fit_grid(img):
    """返回 (x0, y0, 格宽, 格高) (相对 img), 找不到清晰的棋盘网格则返回 None."""
    fx = fit_axis(edge_profile(img, 1))
    fy = fit_axis(edge_profile(img, 0))
    if fx is None or fy is None or min(fx[2], fy[2]) < 5:
        return None
    return fx[0], fy[0], fx[1], fy[1]


def refine_region(cfg):
    """把框选区域收紧到真正的 8x8 棋盘上. 返回 (新配置, 是否成功)."""
    g = fit_grid(Screen(cfg).grab())
    if g is None:
        return cfg, False
    x0, y0, sx, sy = g
    return {"left": cfg["left"] + int(round(x0)), "top": cfg["top"] + int(round(y0)),
            "width": int(round(8 * sx)), "height": int(round(8 * sy))}, True


# --------------------------------------------------------------------------
# 学习模板 (初始局面)
# --------------------------------------------------------------------------
def learn_templates(img, side="auto", path=None):
    cells = split(img)
    if side == "auto":
        top = np.mean([center_gray(cells[r][c]).mean() for r in (0, 1) for c in range(8)])
        bot = np.mean([center_gray(cells[r][c]).mean() for r in (6, 7) for c in range(8)])
        wb = bool(bot > top)
    else:
        wb = side == "white"
    print("棋盘朝向: %s 方在下" % ("白" if wb else "黑"))

    # 空格背景 (第3~6行), 按格子深浅分别取逐像素中位数
    bg = {}
    for par in (0, 1):
        stack = [cells[r][c] for r in range(2, 6) for c in range(8) if (r + c) % 2 == par]
        bg[par] = np.median(np.stack(stack), axis=0).astype(np.uint8)
    mid = sum(1 for r in range(2, 6) for c in range(8) if center_std(cells[r][c]) >= OCC_STD)
    holes = {(r, c) for r in (0, 1, 6, 7) for c in range(8) if center_std(cells[r][c]) < OCC_STD}
    # 允许对手已先走了一步 (执黑时总是这样): 最多 2 格不同, 缺子的格子不参与学习
    if mid > 2 or len(holes) > 2:
        raise ValueError(
            "当前画面不是标准初始局面: 中间 4 行有 %d 格有东西, 首尾 4 行有 %d 格是空的。\n"
            "请开一局全新的对局(不是谜题/练习), 等棋子都摆好、没有弹窗遮挡时再学习\n"
            "(执黑时白方已走第一步没关系);\n"
            "如果确实是开局, 请重新校准棋盘 (网格没对齐也会这样)。" % (mid, len(holes)))
    if holes or mid:
        print("检测到开局已走了一步, 跳过变动的格子, 其余棋子照常学习")
    # 只统计真正的空格 (已走的那个兵所在格不算)
    std_e = max([s for s in (center_std(cells[r][c]) for r in range(2, 6) for c in range(8))
                 if s < OCC_STD] or [0.0])

    board = chess.Board()
    vecs, labels, std_p = [], [], []
    for r in (0, 1, 6, 7):
        for c in range(8):
            if (r, c) in holes:
                continue
            p = board.piece_at(sq_of(r, c, wb))
            cell, par = cells[r][c], (r + c) % 2
            std_p.append(center_std(cell))
            # 棋子轮廓掩码 (填充内部), 用于合成到另一种底色上
            diff = np.abs(cell.astype(int) - bg[par].astype(int)).sum(2)
            mask = (diff > 60).astype(np.uint8) * 255
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
            cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            filled = np.zeros_like(mask)
            cv2.drawContours(filled, cnts, -1, 255, -1)
            m3 = filled[:, :, None] > 0
            for bpar in (0, 1):
                variant = cell if bpar == par else np.where(m3, cell, bg[bpar]).astype(np.uint8)
                vecs.append(unit(gray_feat(variant)))
                labels.append(pidx(p))

    p_min = min(std_p)
    thr = min(max(3 * std_e + 2, 0.4 * p_min), 0.9 * p_min)
    np.savez(path or TPL, vecs=np.array(vecs, np.float32), labels=np.array(labels),
             thr=thr, white_bottom=wb, bg0=bg[0], bg1=bg[1])
    print("已保存 %d 个模板, 占用阈值 %.1f (空格最大 %.1f / 棋子最小 %.1f)" % (len(vecs), thr, std_e, p_min))
    return wb


def border_color(cell):
    b = 5
    px = np.concatenate([cell[:b].reshape(-1, 3), cell[-b:].reshape(-1, 3),
                         cell[:, :b].reshape(-1, 3), cell[:, -b:].reshape(-1, 3)])
    return np.median(px, axis=0)


def normalize_bg(cell, bg):
    """格子底色不是常规的浅/深色 (例如"上一步"黄色高亮) 时, 把棋子抠出来贴回标准底色."""
    col = border_color(cell)
    if np.abs(col - border_color(bg)).sum() < 30:
        return cell
    diff = np.abs(cell.astype(int) - col.astype(int)).sum(2)
    mask = (diff > 60).astype(np.uint8) * 255
    # 高亮底色上的投影偏黄 (B 明显低于 G/R), 棋子本体是灰蓝色, 去掉黄色晕影
    c = cell.astype(int)
    mask[c[:, :, 0] + 25 < np.minimum(c[:, :, 1], c[:, :, 2])] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(mask)
    cv2.drawContours(filled, cnts, -1, 255, -1)
    out = np.where(filled[:, :, None] > 0, cell, bg).astype(np.uint8)
    out[:5], out[-5:], out[:, :5], out[:, -5:] = bg[:5], bg[-5:], bg[:, :5], bg[:, -5:]  # 去掉邻格漏进来的边
    return out


class Recognizer:
    def __init__(self, path=None):
        d = np.load(path or TPL)
        self.vecs, self.labels, self.thr = d["vecs"], d["labels"], float(d["thr"])
        self.bg = (d["bg0"], d["bg1"]) if "bg0" in d.files else None
        self.scores = np.zeros((8, 8), np.float32)

    def grid(self, img):
        cells = split(img)
        g = [[None] * 8 for _ in range(8)]
        for r in range(8):
            for c in range(8):
                cell = cells[r][c]
                self.scores[r][c] = 0
                if center_std(cell) < self.thr:
                    continue
                if self.bg is not None:
                    cell = normalize_bg(cell, self.bg[(r + c) % 2])
                # 棋子在格内的位置会有几像素偏差 (例如走到第 4 行的兵), 做小范围平移取最优
                best, bi = -1.0, 0
                for dy, dx in SHIFTS:
                    if dy or dx:
                        M = np.float32([[1, 0, dx], [0, 1, dy]])
                        cs = cv2.warpAffine(cell, M, (CELL, CELL), borderMode=cv2.BORDER_REPLICATE)
                    else:
                        cs = cell
                    sc = self.vecs @ unit(gray_feat(cs))
                    i = int(sc.argmax())
                    if sc[i] > best:
                        best, bi = float(sc[i]), i
                        if best >= GOOD_SCORE:
                            break
                self.scores[r][c] = best
                if best >= MIN_SCORE:
                    g[r][c] = pdec(int(self.labels[bi]))
        return g


def to_map(grid, wb):
    m = {}
    for r in range(8):
        for c in range(8):
            if grid[r][c] is not None:
                m[sq_of(r, c, wb)] = grid[r][c]
    return m


def is_opening(grid):
    """屏幕是否是新对局的开局: 初始局面, 或白方已走完第一步 (我方执黑时看到的就是后者,
    只认纯初始局面的话, 执黑永远等不到新对局)."""
    return opening_mismatch(to_map(grid, True)) == 0 or opening_mismatch(to_map(grid, False)) == 0


def orientation(grid, forced="auto"):
    if forced != "auto":
        return forced == "white"
    w = sum(1 for r in (6, 7) for p in grid[r] if p is not None and p.color)
    b = sum(1 for r in (6, 7) for p in grid[r] if p is not None and not p.color)
    return w >= b


# --------------------------------------------------------------------------
# 局面比对
# --------------------------------------------------------------------------
def mismatch(m, board):
    return sum(1 for sq in chess.SQUARES if m.get(sq) != board.piece_at(sq))


def opening_mismatch(m):
    """屏幕局面 m 与「初始局面, 或白方已走一步」的最小差异格数 (执黑时白方先走)."""
    b = chess.Board()
    best = mismatch(m, b)
    for mv in list(b.legal_moves):
        b.push(mv)
        best = min(best, mismatch(m, b))
        b.pop()
    return best


def find_move(m, board):
    """在 board 的合法着法里找出使屏幕局面 m 最吻合的一步 (容忍 1 格识别误差)."""
    cur = mismatch(m, board)
    if cur < 2:
        return None
    res = []
    for mv in list(board.legal_moves):
        board.push(mv)
        res.append((mismatch(m, board), mv))
        board.pop()
    res.sort(key=lambda t: t[0])
    if res and res[0][0] < cur and res[0][0] <= 1 and (len(res) == 1 or res[1][0] > res[0][0]):
        return res[0][1]
    return None


def board_from_map(m, turn):
    if m == START_MAP:
        return chess.Board()
    b = chess.Board(None)
    b.set_piece_map(m)
    b.turn = turn
    b.castling_rights = chess.BB_CORNERS
    b.clean_castling_rights()
    return b if b.is_valid() else None


class Resync(Exception):
    pass


class NewGame(Exception):
    pass


class Stopped(Exception):
    pass


def release_mouse():
    """无论 pyautogui 处于什么状态, 都确保鼠标左键已松开."""
    try:
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
    except Exception:
        pass


# --------------------------------------------------------------------------
# 自动对战
# --------------------------------------------------------------------------
class Bot:
    def __init__(self, args, stop_evt=None, on_status=None):
        self.args = args
        self.stop_evt = stop_evt or threading.Event()
        self.on_status = on_status
        with open(CFG, encoding="utf-8") as f:
            self.cfg = json.load(f)
        self.sc = Screen(self.cfg)
        self.rec = Recognizer()
        self.engine = chess.engine.SimpleEngine.popen_uci(ENGINE)
        self.engine.configure({"Threads": args.threads, "Hash": args.hash})
        self.wb = True
        self.us = chess.WHITE
        self.on_board = None  # 回调 (fen, 白方是否在下), 供图形界面显示对局
        self.moves_log = []   # 本局每步记录: {move, who, cp, t, note, loss}
        self.learned = {}      # 从历史棋谱学到的路线 (局面 -> 着法集合)
        self.spent = 0         # 本局为防和/防简化已让出的评分 (厘兵)
        self.prev_eval = None  # 我方上一步走完后的评分 (我方视角), 用来估算对手那步的损失
        self.flat = 0          # 已连续多少步所有候选都接近 0.00
        self.auto = bool(getattr(args, "auto", False))  # 自动模式: 智能思考 + 自动收书; 否则按手动参数
        self.book = BOOK if self.auto else BOOK_SIMPLE
        # 收录: 把本局存进 games/ 并在开局使用从获胜棋谱学到的对手库. 自动模式恒开, 手动模式由 args.learn 决定
        self.learn = self.auto or bool(getattr(args, "learn", False))

    def close(self):
        try:
            self.engine.quit()
        except Exception:
            pass

    def check_stop(self):
        if self.stop_evt.is_set():
            raise Stopped()

    def status(self, text):
        if self.on_status:
            self.on_status(text)

    def emit(self, board):
        if self.on_board:
            self.on_board(board.fen(), self.wb)

    # ---- 屏幕读取 ----
    def read(self):
        """返回连续两帧一致(棋子不在动画中)的识别结果."""
        last = None
        while True:
            self.check_stop()
            g = self.rec.grid(self.sc.grab())
            if g == last:
                return g
            last = g
            time.sleep(0.06)

    def new_game(self, mode):
        t0 = time.time()
        warned = False
        self.status("等待棋盘…")
        while True:
            self.check_stop()
            g = self.read()
            self.wb = orientation(g, self.args.color)
            self.us = chess.WHITE if self.wb else chess.BLACK
            m = to_map(g, self.wb)
            if m == START_MAP:
                return chess.Board()
            turn = self.us if (mode == "us" or self.args.turn == "us") else (not self.us)
            b = board_from_map(m, turn)
            if b is not None:
                print("从当前局面接管: %s" % b.fen())
                return b
            if not warned and time.time() - t0 > 3:
                print("还没识别到有效棋盘 (被弹窗遮挡? 请运行 check 查看识别结果)...")
                warned = True
            time.sleep(0.4)

    def wait_start(self):
        print("等待新对局 (棋盘回到初始局面)...")
        self.status("对局结束, 等待新对局…")
        while not is_opening(self.read()):
            self.check_stop()
            time.sleep(0.4)

    # ---- 鼠标 ----
    def center(self, sq):
        r, c = rc_of(sq, self.wb)
        return (self.cfg["left"] + (c + 0.5) * self.cfg["width"] / 8,
                self.cfg["top"] + (r + 0.5) * self.cfg["height"] / 8)

    @staticmethod
    def glide(x1, y1, x2, y2, steps=8, dt=0.008):
        for i in range(1, steps + 1):
            pyautogui.moveTo(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
            time.sleep(dt)

    def drag(self, mv):
        x1, y1 = self.center(mv.from_square)
        x2, y2 = self.center(mv.to_square)
        pyautogui.moveTo(x1, y1)
        time.sleep(0.03)
        if self.args.click:
            pyautogui.click()
            time.sleep(0.06)
            self.glide(x1, y1, x2, y2, 4, 0.004)
            pyautogui.click()
            return
        pyautogui.mouseDown()
        try:
            time.sleep(0.04)
            self.glide(x1, y1, x2, y2)
            time.sleep(0.03)
        finally:
            release_mouse()

    # ---- 走棋 ----
    def confirm(self, after, mv, timeout=2.0):
        """等待屏幕出现我方走完后的局面 (或对手已经应对)."""
        t0 = time.time()
        clicked = False
        while time.time() - t0 < timeout:
            self.check_stop()
            m = to_map(self.rec.grid(self.sc.grab()), self.wb)
            if mismatch(m, after) <= 1 or find_move(m, after) is not None:
                return True
            if mv.promotion and not clicked and time.time() - t0 > 0.5:
                x, y = self.center(mv.to_square)  # 有升变选择框时, 后手点一下目标格(通常是后)
                pyautogui.click(x, y)
                clicked = True
            time.sleep(0.05)
        return False

    @staticmethod
    def is_drawish(board, mv):
        """走 mv 之后是否直接导致/逼近和棋 (重复局面、50 步、子力不足、逼和)."""
        b = board.copy()  # 保留着法栈, 才能判断重复
        b.push(mv)
        return (b.is_repetition(2) or b.is_fifty_moves() or b.halfmove_clock >= 90
                or b.is_insufficient_material() or b.is_stalemate())

    @staticmethod
    def heavy_count(board, colors=(chess.WHITE, chess.BLACK)):
        """指定颜色的非兵子力加权数量 (后算 2), 用来衡量一步棋是否在换子简化."""
        w = {chess.QUEEN: 2, chess.ROOK: 1, chess.BISHOP: 1, chess.KNIGHT: 1}
        return sum(n * len(board.pieces(pt, c)) for pt, n in w.items() for c in colors)

    def trade_cost(self, board, pv):
        """沿主变前三步 (我方一步 + 对方应对 + 我方回吃) 里, 我方每掉一个非兵子力就扣分.
        等价交换 (双方都掉) 和白送子 (只有我方掉) 都扣; 单方面白吃子 (只有对方掉) 不扣.
        评分同分时, 这样会优先选不丢子的着法, 不会为了凑重复去弃车."""
        b = board.copy(stack=False)
        me = (self.us,)
        my0 = self.heavy_count(b, me)
        for m in pv[:3]:
            b.push(m)
        lost_me = max(0, my0 - self.heavy_count(b, me))
        return lost_me * TRADE_COST

    def _cp(self, info):
        return info["score"].pov(self.us).score(mate_score=100000)

    @staticmethod
    def allowed_moves(board):
        """可供引擎选择的着法. 升变选择框只能点出后, 所以不考虑低升变 (否则引擎以为升了车, 屏幕上却是后,
        会把"升车避免逼和"走成真逼和). 同时排除走完会逼和对手的着法 (除非别无选择)."""
        legal = [m for m in board.legal_moves if not m.promotion or m.promotion == chess.QUEEN]
        ok = []
        for m in legal:
            board.push(m)
            stale = board.is_stalemate()
            board.pop()
            if not stale:
                ok.append(m)
        return ok or legal

    def pick_move(self, board):
        """返回 (着法, 所选步评分, 最优步评分, 备注), 评分均为我方视角厘兵.
        先用单线搜索判断局势; 劣势或有杀棋时直接走最优步 (和棋是好结果).
        局势不差时再用 MultiPV, 在评分接近的候选里避开和棋走法, 均势时还尽量不换子简化."""
        t = self.args.time
        self.last_cands = []
        self.last_depth = None
        rm = self.allowed_moves(board)
        if getattr(self.args, "avoid_draw", True):
            epd = board.epd()
            for src, label in ((self.learned, "对手库"), (self.book[self.us], "开局库")):
                cand = src.get(epd, ())
                weights = cand if isinstance(cand, dict) else {u: 1 for u in cand}  # 对手库带胜局数
                hits = []
                for u, n in weights.items():
                    m = chess.Move.from_uci(u)
                    if m in board.legal_moves and not self.is_drawish(board, m):  # 书本着法也不许造成重复/和棋
                        hits.append((n, m))
                if hits:
                    top = max(n for n, _ in hits)  # 胜局数最多的优先, 并列才随机
                    return random.choice([m for n, m in hits if n == top]), None, None, label
        only_one = len(rm) == 1
        if only_one or not getattr(self.args, "avoid_draw", True):
            # 只有一步可走时不必多想 (仍搜一小会儿拿评分, 用来估算对手上一步的损失)
            info = self.engine.analyse(board, chess.engine.Limit(time=min(t, 0.1) if only_one else t), root_moves=rm)
            self.last_depth = info.get("depth")
            cp = self._cp(info)
            return info["pv"][0], cp, cp, ""
        # 只做一次 MultiPV 搜索, 总用时 = t (第 1 线的评分就是最优评分, 不再另做单线搜索)
        t_full = t
        flat_mode = self.auto and self.flat >= FLAT_MOVES  # 死和局: 连续多步候选都是 0.00, 缩短思考时间
        if flat_mode:
            t = FLAT_TIME
        cands = self._multipv(board, rm, chess.engine.Limit(time=t))
        if cands and flat_mode and not self._is_flat(cands):  # 出现不平的候选: 按正常时间重搜
            t = t_full
            cands = self._multipv(board, rm, chess.engine.Limit(time=t))
        if cands:
            self.flat = self.flat + 1 if self._is_flat(cands) else 0
        if not cands:
            info = self.engine.analyse(board, chess.engine.Limit(time=0.2), root_moves=rm)
            cp = self._cp(info)
            return info["pv"][0], cp, cp, ""
        for deeper in (False, True):
            best_cp, best_mv, _ = cands[0]
            if best_cp >= 90000:
                return best_mv, best_cp, best_cp, ""
            if best_cp < BAD_CP:  # 劣势: 追加半份时间做单线搜索, 把最优步找准 (和棋是好结果)
                info = self.engine.analyse(board, chess.engine.Limit(time=t * 0.5), root_moves=rm)
                self.last_depth = info.get("depth")
                cp = self._cp(info)
                return info["pv"][0], cp, cp, ""
            left = (DRAW_BUDGET - self.spent) if self.auto else DRAW_WINDOW  # 手动模式不设累计预算
            if best_cp <= 150 and left <= 0:  # 本局让出的评分已达上限: 不再为防和/防简化牺牲
                return best_mv, best_cp, best_cp, ""
            mv, cp = self._select(board, cands, left)
            if mv == best_mv or deeper or not self.auto or (self.last_depth or 0) >= MIN_DEPTH:
                break
            # 打算偏离最优步, 但深度还浅 (分差多半是噪声): 先加深再决定
            deep = self._multipv(board, rm, chess.engine.Limit(time=t * 2, depth=MIN_DEPTH))
            if not deep:
                break
            cands = deep
        note = ""
        if mv != best_mv:
            note = "改走 %s (评分 %s, 最优 %s)" % (board.san(mv), fmt_score(cp), fmt_score(best_cp))
            print("避开和棋/简化: " + note)
        if best_cp <= 150:
            self.spent += max(0, best_cp - cp)
        return mv, cp, best_cp, note

    @staticmethod
    def _is_flat(cands):
        """只看最优步: 最优评分接近 0.00 就算平 (其它候选很差也不影响, 只要最优步稳在 0.00)."""
        return abs(cands[0][0]) <= FLAT_CP

    def _multipv(self, board, rm, limit):
        """MultiPV 搜索, 返回 [(评分, 首步, 主变)] (我方视角, 由强到弱), 并记录搜索深度."""
        infos = self.engine.analyse(board, limit, multipv=MULTIPV_N, root_moves=rm)
        cands = [(self._cp(i), i["pv"][0], i["pv"]) for i in infos if i.get("pv") and "score" in i]
        if cands:
            self.last_depth = infos[0].get("depth")
        return cands

    def _select(self, board, cands, left):
        """在候选里避开和棋步, 均势时再轻罚换子. 返回 (着法, 评分); 候选标签写入 self.last_cands.
        left: 本局还能让出的评分预算 (厘兵)."""
        best_cp, best_mv, _ = cands[0]
        self.last_cands = []
        if best_cp <= 150:
            floor = best_cp - max(0, min(DRAW_WINDOW, left))
        else:  # 优势很大: 允许让出更多换取不和棋, 但仍要保持明显优势
            floor = max(best_cp - 80, 100)
        simplify = SIMPLIFY_MIN <= best_cp <= SIMPLIFY_MAX  # 均势: 不主动换子
        scored = []
        for cp, mv, pv in cands:
            tc = min(self.trade_cost(board, pv), SIMPLIFY_WINDOW) if simplify else 0
            tag = "%s %s" % (board.san(mv), fmt_score(cp))
            if tc:
                tag += " 换子-%d" % tc
            drawish = self.is_drawish(board, mv)
            if cp < floor:
                tag += " 低于窗口"
            elif drawish:
                tag += " 和棋步"
            self.last_cands.append(tag)
            if cp < floor or drawish:
                continue
            scored.append((cp - tc, cp, mv))
        if not scored:
            return best_mv, best_cp
        _, cp, mv = max(scored, key=lambda s: s[0])  # 并列时取排在前面的 (更强的) 候选
        return mv, cp

    def my_move(self, board):
        self.status("思考中…")
        t0 = time.time()
        mv, cp, best_cp, note = self.pick_move(board)
        dt = time.time() - t0
        self.check_stop()
        # 刚离开书本: 上一步是书本着法, 没有评分. 对"我方书本着法走完后"的局面补一次短搜索, 给 prev_eval 一个值
        if (best_cp is not None and self.prev_eval is None and self.moves_log
                and self.moves_log[-1]["who"] == "opp" and board.move_stack):
            try:
                prev = board.copy()
                prev.pop()  # 撤销对手那一步 -> 我方书本着法走完后的局面
                info = self.engine.analyse(prev, chess.engine.Limit(time=min(self.args.time, 0.3)))
                self.prev_eval = self._cp(info)
            except Exception:
                pass
        # 对手上一步的损失 = 现在的最优评分 - 我方上一步走完后的评分 (只是估计, 杀棋分不计)
        if (best_cp is not None and self.prev_eval is not None and self.moves_log
                and self.moves_log[-1]["who"] == "opp" and abs(best_cp) < 90000 and abs(self.prev_eval) < 90000):
            self.moves_log[-1]["loss"] = best_cp - self.prev_eval
        san = board.san(mv)
        after = board.copy(stack=False)
        after.push(mv)
        self.status("我方走子: %s" % san)
        for _ in range(3):
            self.check_stop()
            self.drag(mv)
            if self.confirm(after, mv):
                print("我方: %s (%s, %.1fs)" % (san, "书" if cp is None else fmt_score(cp, True), dt))
                board.push(mv)
                self.moves_log.append({"who": "us", "san": san, "cp": cp, "t": dt, "note": note,
                                       "cands": list(self.last_cands), "depth": self.last_depth})
                self.prev_eval = cp
                return
        print("走子 %s 没有在屏幕上生效, 重新同步" % san)
        raise Resync()

    def save_game(self, board):
        """把本局存成 PGN (games/ 目录), 每步带评分/用时, 对手步带估算的评分损失."""
        if not board.move_stack or not self.learn:  # 手动模式未勾选"收录棋谱"时不存档
            return
        try:
            game = chess.pgn.Game()
            if self.start_board.fen() != chess.STARTING_FEN:
                game.setup(self.start_board)
            o = game_end(board)
            result = o.result() if o else "*"
            game.headers.update({
                "Event": "Duolingo", "Site": "duolingo.cn", "Date": time.strftime("%Y.%m.%d"),
                "White": "Bot" if self.us else "Opponent", "Black": "Opponent" if self.us else "Bot",
                "Result": result, "Termination": o.termination.name if o else "unfinished"})
            node = game
            for mv, e in zip(board.move_stack, self.moves_log):
                node = node.add_variation(mv)
                if e["who"] == "us":
                    dep = " d%d" % e["depth"] if e.get("depth") else ""
                    node.comment = ("%s%s %.1fs %s" % (fmt_score(e["cp"], True), dep, e["t"], e["note"])).strip()
                    if e.get("cands"):  # 候选步 (厘兵), 看防和/防简化到底有没有起作用
                        node.comment += " | 候选: " + "; ".join(e["cands"])
                elif e.get("loss") is not None:
                    node.comment = "loss~%d" % e["loss"]
            os.makedirs(GAMES, exist_ok=True)
            name = "%s_%s.pgn" % (time.strftime("%Y%m%d_%H%M%S"), result.replace("/", "-").replace("*", "unfinished"))
            path = os.path.join(GAMES, name)
            with open(path, "w", encoding="utf-8") as f:
                print(game, file=f, end="\n\n")
            losses = [e["loss"] for e in self.moves_log if e.get("loss") is not None]
            print("棋谱已保存: %s" % path)
            if losses:  # 带符号统计: 负值是估算噪声, 不截断, 否则平均值会系统性偏高
                big = sum(1 for x in losses if x >= LOSS_BIG)
                print("对手损失估算 (仅书本之外的 %d 步): 平均 %+.1f 厘兵, 中位数 %+d, 最大 %d, 损失 >= %d 的有 %d 步" % (
                    len(losses), sum(losses) / len(losses), sorted(losses)[len(losses) // 2],
                    max(losses), LOSS_BIG, big))
        except Exception as ex:  # 存档失败不能影响对局
            print("保存棋谱失败: %s" % ex)

    def wait_opp(self, board):
        last, t0, said = None, time.time(), 0
        self.status("等待对手…")
        while True:
            self.check_stop()
            g = self.rec.grid(self.sc.grab())
            if g == last:
                m = to_map(g, self.wb)
                mv = find_move(m, board)
                if mv is not None:
                    return mv
                # 屏幕回到开局 (初始局面或白方刚走一步) 而我们记的棋盘不是这样 -> 新对局
                if is_opening(g) and mismatch(m, board) >= 2:
                    raise NewGame()
            last = g
            if time.time() - t0 > 20 * (said + 1):
                said += 1
                print("仍在等待对手... (棋盘被遮挡或识别异常时请运行 check)")
            time.sleep(0.08)

    def play_game(self, board):
        print("我方执 %s, 开始" % ("白" if self.us else "黑"))
        self.status("对局开始, 我方执%s" % ("白" if self.us else "黑"))
        self.start_board = board.copy(stack=False)
        self.moves_log, self.prev_eval, self.spent, self.flat = [], None, 0, 0
        self.learned, n = load_learned(self.us) if self.learn else ({}, 0)  # 开启收录时才使用对手库
        if n:
            print("从 %d 局获胜棋谱学到 %d 个局面的路线" % (n, len(self.learned)))
        try:
            while game_end(board) is None:
                self.check_stop()
                self.emit(board)
                if board.turn == self.us:
                    self.my_move(board)
                else:
                    mv = self.wait_opp(board)
                    print("对手: %s" % board.san(mv))
                    self.moves_log.append({"who": "opp", "san": board.san(mv)})
                    board.push(mv)
        finally:
            self.save_game(board)  # 正常结束、中途停止、重新同步都会存档
        self.emit(board)
        o = game_end(board)
        print("对局结束: %s (%s)" % (o.result() if o else "*", o.termination.name if o else "?"))
        self.wait_start()

    def run(self):
        mode = "args"
        try:
            while True:
                board = self.new_game(mode)
                mode = "us"
                try:
                    self.play_game(board)
                except Resync:
                    self.status("走子未生效, 重新同步…")
                    time.sleep(0.5)
                except NewGame:
                    print("检测到新对局")
        except Stopped:
            self.status("已停止")
        finally:
            release_mouse()


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------
def load_cfg():
    if not os.path.exists(CFG):
        raise SystemExit("还没有校准棋盘, 请先运行: python duolingo_chess.py calibrate")
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def draw_grid(img):
    out = img.copy()
    h, w = out.shape[:2]
    for i in range(9):
        cv2.line(out, (round(i * w / 8), 0), (round(i * w / 8), h), (0, 0, 255), 1)
        cv2.line(out, (0, round(i * h / 8)), (w, round(i * h / 8)), (0, 0, 255), 1)
    return out


def cmd_calibrate(args):
    pts = []
    for name in ("左上角", "右下角"):
        print("请在 %d 秒内把鼠标移到【棋盘 %s】(棋盘最外沿)..." % (args.delay, name))
        for i in range(args.delay, 0, -1):
            print(i, end=" ", flush=True)
            time.sleep(1)
        p = pyautogui.position()
        pts.append((p.x, p.y))
        print("-> (%d, %d)" % (p.x, p.y))
    (x1, y1), (x2, y2) = pts
    w, h = x2 - x1, y2 - y1
    if w < 80 or h < 80:
        raise SystemExit("框选区域太小, 请重试")
    if abs(w - h) > 0.03 * max(w, h):
        print("警告: 宽 %d 高 %d 不是正方形, 请确认两个角点" % (w, h))
    cfg = {"left": x1, "top": y1, "width": w, "height": h}
    cfg, fitted = refine_region(cfg)
    if fitted:
        print("已自动贴合 8x8 网格: %s" % cfg)
    else:
        print("未能自动识别网格, 沿用框选区域")
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    cv2.imwrite(os.path.join(HERE, "board_preview.png"), draw_grid(Screen(cfg).grab()))
    print("已保存 %s; 请打开 board_preview.png 确认红色网格与棋盘格子对齐" % CFG)


def cmd_learn(args):
    img = Screen(load_cfg()).grab()
    cv2.imwrite(os.path.join(HERE, "learn_board.png"), img)
    try:
        wb = learn_templates(img, args.side)
    except ValueError as e:
        raise SystemExit(str(e))
    g = Recognizer().grid(img)
    bad = opening_mismatch(to_map(g, wb))
    print("自检: 开局识别误差 %d 格" % bad)
    if bad:
        print("警告: 当前画面不是标准初始局面, 或网格没对齐, 请检查 learn_board.png")


def cmd_check(args):
    img = Screen(load_cfg()).grab()
    rec = Recognizer()
    g = rec.grid(img)
    wb = orientation(g, args.color)
    for r in range(8):
        print(" ".join(g[r][c].symbol() if g[r][c] else "." for c in range(8)))
    m = to_map(g, wb)
    b = board_from_map(m, chess.WHITE if wb else chess.BLACK)
    print("朝向: %s方在下 | 匹配分最低的有子格: %.2f" % ("白" if wb else "黑", min(
        [rec.scores[r][c] for r in range(8) for c in range(8) if g[r][c]] or [0])))
    print("FEN: %s" % (b.fen() if b else "(无效局面)"))
    out = draw_grid(img)
    h, w = out.shape[:2]
    for r in range(8):
        for c in range(8):
            if g[r][c]:
                cv2.putText(out, g[r][c].symbol(), (round(c * w / 8) + 4, round((r + 1) * h / 8) - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 255), 2)
    cv2.imwrite(os.path.join(HERE, "check.png"), out)
    print("已输出 check.png")


def cmd_play(args):
    load_cfg()
    if not os.path.exists(TPL):
        raise SystemExit("还没有棋子模板, 请先运行: python duolingo_chess.py learn")
    pyautogui.PAUSE = 0
    pyautogui.FAILSAFE = True
    if getattr(args, "auto", False):
        args = auto_args()
    bot = Bot(args)
    print("引擎已就绪 (Threads=%d Hash=%dMB, 每步 %.2fs). 鼠标甩到屏幕左上角可中止." % (
        args.threads, args.hash, args.time))
    try:
        bot.run()
    except KeyboardInterrupt:
        print("已停止")
    except pyautogui.FailSafeException:
        print("触发 FAILSAFE, 已中止")
    finally:
        bot.close()


def main():
    ap = argparse.ArgumentParser(description="Duolingo 国际象棋自动对战")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("calibrate", help="框选棋盘区域")
    p.add_argument("--delay", type=int, default=5, help="每个角点的倒计时秒数")
    p.set_defaults(fn=cmd_calibrate)

    p = sub.add_parser("learn", help="在初始局面学习棋子外观")
    p.add_argument("--side", choices=["auto", "white", "black"], default="auto", help="你执哪方(棋盘下方)")
    p.set_defaults(fn=cmd_learn)

    p = sub.add_parser("check", help="识别当前棋盘并输出调试图")
    p.add_argument("--color", choices=["auto", "white", "black"], default="auto")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("play", help="自动对战")
    p.add_argument("--time", type=float, default=1.0, help="每步思考秒数")
    p.add_argument("--allow-draw", dest="avoid_draw", action="store_false", help="不主动避开和棋走法")
    p.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    p.add_argument("--hash", type=int, default=512, help="哈希表 MB")
    p.add_argument("--color", choices=["auto", "white", "black"], default="auto", help="你执哪方(默认按棋盘下方自动判断)")
    p.add_argument("--turn", choices=["us", "them"], default="us", help="中途接管时, 当前轮到谁走")
    p.add_argument("--click", action="store_true", help="用点击-点击代替拖动")
    p.add_argument("--learn", action="store_true", help="手动模式下也把棋谱存进 games/ 并使用对手库 (自动模式恒开)")
    p.add_argument("--auto", action="store_true", help="自动模式: 忽略其他参数, 智能思考并自动收录获胜棋谱")
    p.set_defaults(fn=cmd_play)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
