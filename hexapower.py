#!/usr/bin/env python3
"""HEXA POWER — CardputerZero の消費電力を見ながら、電気を食う機能を ON/OFF するアプリ (v0.1)

起動するとハニカムの画面。左に6つの六角形、右に今の消費電力とグラフ。
  1 BT      Bluetooth                 ENTER で ON/OFF
  2 HDMI    HDMI 出力                 ENTER で ON/OFF
  3 LAN     有線LANチップ (USB)       ENTER で ON/OFF
  4 CPU     標準 / エコ(2コア) / 最小(2コア+最低クロック)   ENTER で切替
  5 Wi-Fi   Wi-Fi の省電力            ENTER で ON/OFF
  6 起動時  上の設定を起動時にも当てる  ENTER で ON/OFF
切り替えると約15秒あとに「切替前との差」を出す (電池で動いている時だけ測れる)

消費電力は本体の残量計 BQ27220 (I2C 0x55) の値。USB / EXT から給電中は
本体は外の電気で動くので、電池側からは消費が測れない (画面にそう出る)。

キー: 方向 = F(↑) X(↓) Z(←) C(→) / 1〜6 = 直接選ぶ / ENTER = 切替 / ESC = 終了
usage:
  hexapower                 # 通常起動 (ランチャーのアイコンと同じ)
  hexapower --probe         # 電池の値の読み方と、各機能が見つかるかを表示して終了
  hexapower --sim OUTDIR    # PC 用: 偽の画面/キー/電池で動かし、画面を PNG に保存
"""
import argparse
import collections
import fcntl
import glob
import json
import math
import os
import random
import select
import signal
import struct
import subprocess
import sys
import time

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

VERSION = "0.1.1"
LCD_W, LCD_H = 320, 170
TOP_H, BOT_H = 18, 16
BG, FG, SUB = (14, 16, 22), (235, 238, 245), (150, 158, 175)
ACC = (70, 190, 255)                      # HEXA POWER の強調色 (CLIP=緑 / MATRIX=琥珀)
ACC_DIM = (20, 44, 60)
OFFC = (255, 150, 60)                     # OFF にしている (節電中) 色
NA = (70, 76, 90)

CTL = os.environ.get("HEXAPOWER_CTL", "/usr/local/sbin/hexapower-ctl")
DELTA_WAIT, DELTA_SPAN = 5.0, 10.0        # 切替後 5秒待って 10秒の平均を比べる
SAMPLE_SEC = 1.0
HIST_N = 150                              # グラフに出す秒数

ITEMS = [  # key, 六角形の名前, 説明
    ("bt", "BT", "Bluetooth。使わないなら OFF"),
    ("hdmi", "HDMI", "HDMI 出力。モニタをつながないなら OFF"),
    ("lan", "LAN", "有線LANチップ。LANケーブルを使わないなら OFF"),
    ("cpu", "CPU", "標準→エコ(2コア)→最小(最低クロック)"),
    ("wifips", "Wi-Fi", "Wi-Fi の省電力。ON で節電 (反応が少し遅くなる)"),
    ("boot", "起動時", "ここでの設定を、次の起動時にも当てる"),
]
CPU_NAMES = {"std": "標準", "eco": "エコ", "min": "最小", "custom": "独自"}
CPU_NEXT = {"std": "eco", "eco": "min", "min": "std", "custom": "std"}


# ================================================================ keys (HEXA CLIP と同じ)
EVIOCGRAB = 0x40044590
EV_FMT = "llHHi"
EV_SIZE = struct.calcsize(EV_FMT)
K_ESC, K_BS, K_TAB, K_ENTER = 1, 14, 15, 28
K_UP, K_LEFT, K_RIGHT, K_DOWN = 103, 105, 106, 108
DIGITS = {2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8", 10: "9", 11: "0"}
LETTERS = {33: "f", 45: "x", 44: "z", 46: "c", 19: "r", 16: "q"}
ACTIONS = {K_UP: "UP", K_DOWN: "DOWN", K_LEFT: "LEFT", K_RIGHT: "RIGHT", K_TAB: "TAB",
           K_ENTER: "ENTER", K_ESC: "ESC", K_BS: "BS"}
LETTER_NAV = {"f": "UP", "x": "DOWN", "z": "LEFT", "c": "RIGHT"}


class Keys:
    """キーボードを横取りして ('ACT','LEFT') / ('CHAR','1') に変換。fn+F/X/Z/C も方向"""

    def __init__(self, dev):
        self.fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 1)
        except OSError as e:
            os.close(self.fd)
            if e.errno == 16:
                raise SystemExit("キーボードが使用中です (ほかのアプリが起動中)")
            raise

    def _raw(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        try:
            data = os.read(self.fd, EV_SIZE * 64)
        except BlockingIOError:
            return []
        out = []
        for i in range(0, len(data) - EV_SIZE + 1, EV_SIZE):
            _, _, etype, code, value = struct.unpack(EV_FMT, data[i:i + EV_SIZE])
            if etype == 1 and value == 1:
                out.append(code)
        return out

    def poll(self, timeout=0.0):
        ev = []
        for code in self._raw(timeout):
            if code in ACTIONS:
                ev.append(("ACT", ACTIONS[code]))
            elif code in DIGITS:
                ev.append(("CHAR", DIGITS[code]))
            elif code in LETTERS:
                ch = LETTERS[code]
                ev.append(("ACT", LETTER_NAV[ch]) if ch in LETTER_NAV else ("CHAR", ch))
        return ev

    def flush(self, sec=0.4):
        end = time.time() + sec
        while time.time() < end:
            self._raw(0.05)

    def close(self):
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 0)
        finally:
            os.close(self.fd)


def find_keyboard():
    name = None
    for line in open("/proc/bus/input/devices"):
        if line.startswith("N: Name="):
            name = line.strip().split("=", 1)[1].strip('"')
        elif line.startswith("H: Handlers=") and name and "tca8418" in name:
            for h in line.split("=", 1)[1].split():
                if h.startswith("event"):
                    return "/dev/input/" + h
    raise RuntimeError("keyboard (tca8418) not found")


# ================================================================ display (HEXA CLIP と同じ)
class FrameBuffer:
    def __init__(self, dev="/dev/fb0"):
        import numpy as np
        self.np = np
        base = f"/sys/class/graphics/{os.path.basename(dev)}"
        self.bpp = int(open(f"{base}/bits_per_pixel").read())
        try:
            self.stride = int(open(f"{base}/stride").read())
        except OSError:
            self.stride = LCD_W * self.bpp // 8
        self.f = open(dev, "r+b", buffering=0)

    def show(self, img):
        np = self.np
        a = np.asarray(img.convert("RGB"), dtype=np.uint16)
        if self.bpp == 16:
            buf = (((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)).astype("<u2")
        else:
            buf = np.zeros((LCD_H, LCD_W, 4), np.uint8)
            buf[..., 0], buf[..., 1], buf[..., 2], buf[..., 3] = a[..., 2], a[..., 1], a[..., 0], 255
        row = LCD_W * self.bpp // 8
        raw = buf.tobytes()
        if self.stride == row:
            self.f.seek(0)
            self.f.write(raw)
        else:
            for y in range(LCD_H):
                self.f.seek(y * self.stride)
                self.f.write(raw[y * row:(y + 1) * row])

    def clear(self):
        self.show(Image.new("RGB", (LCD_W, LCD_H)))


JP_TEST = "、。あいえかがくけこしすせそただつてでなにのはぶほまみめもらりるれわをんアエキクケコタチッドニブプボモリルロ・ー上下中了今低体使値六出分切前力動半反取可向回変外失字定実小少差常当形後応戻接推換敗文方時替最有本標横次残段池消測準独用直省確秒節約終給線自色行見角設許認読費起通遅選部量間限電"  # 画面に出る字 (□にならないフォントだけ使う)
_FONT_FILE = {}


def font_covers(font, text=JP_TEST):
    try:
        nd = font.getmask("\U0010FFFD")
        nd_key = (nd.size, bytes(nd))
        for ch in text:
            m = font.getmask(ch)
            if m.getbbox() is None or (m.size, bytes(m)) == nd_key:
                return False
        return True
    except Exception:
        return False


def font_candidates():
    cands = []
    for p in ["/usr/share/APPLaunch/**/*.tt[fc]", "/usr/share/APPLaunch/**/*.otf",
              "/usr/share/fonts/**/*CJK*", "/usr/share/fonts/**/*[Ii][Pp][Aa]*",
              "/usr/share/fonts/**/*[Jj][Pp]*", "/usr/share/fonts/**/*wqy*",
              "/usr/share/fonts/**/*Droid*Fallback*"]:
        cands += glob.glob(p, recursive=True)
    return sorted(set(cands))


def find_font(size, bold=False):
    f = _FONT_FILE.get(bold)
    if f:
        return ImageFont.truetype(f, size)

    def rank(f):
        n = os.path.basename(f).lower()
        if "serif" in n or "mono" in n:
            return 50
        order = (["bold", "85", "heavy", "75", "black", "medium", "65", "regular", "55", "light", "thin"]
                 if bold else ["medium", "65", "regular", "55", "bold", "75", "black", "light", "thin"])
        for i, w in enumerate(order):
            if w in n:
                return i
        return 5
    for f in sorted(font_candidates(), key=rank):
        try:
            font = ImageFont.truetype(f, size)
        except Exception:
            continue
        if font_covers(font):
            _FONT_FILE[bold] = f
            return font
    return ImageFont.load_default()


def hexpts(cx, cy, r, rot=30):
    return [(cx + r * math.cos(math.radians(60 * i + rot)),
             cy + r * math.sin(math.radians(60 * i + rot))) for i in range(6)]


def honeycomb_bg():
    img = Image.new("RGB", (LCD_W, LCD_H), BG)
    d = ImageDraw.Draw(img)
    for row in range(-1, 8):
        for col in range(-1, 12):
            d.polygon(hexpts(col * 30 + (15 if row % 2 else 0), 20 + row * 26, 17), outline=(20, 24, 34))
    return img


def glow_hex(img, cx, cy, r, col):
    g = Image.new("RGB", img.size, (0, 0, 0))
    pts = hexpts(cx, cy, r)
    ImageDraw.Draw(g).line(pts + [pts[0]], fill=col, width=6)
    g = g.filter(ImageFilter.GaussianBlur(3))
    img.paste(ImageChops.lighter(img, g))
    ImageDraw.Draw(img).line(pts + [pts[0]], fill=col, width=2)


# ================================================================ battery (BQ27220)
I2C_SLAVE_FORCE = 0x0706
BQ_ADDR = 0x55
BQ = {"temp": 0x06, "volt": 0x08, "flags": 0x0A, "cur": 0x0C, "rem": 0x10, "full": 0x12,
      "avgcur": 0x14, "tte": 0x16, "avgpow": 0x24, "soc": 0x2C}


class Battery:
    """電池の値。sysfs (power_supply) があればそちら、無ければ I2C で BQ27220 を直接読む
    read() → dict(v=V, i=A(放電がマイナス), p=W(消費, 放電中のみ), soc=%, on_bat=bool, tte=分, temp=℃)"""

    def __init__(self):
        self.ps = None
        self.i2c = None
        for d in sorted(glob.glob("/sys/class/power_supply/*")):
            if open_text(d + "/type") == "Battery" and open_text(d + "/voltage_now"):
                self.ps = d
                break
        if not self.ps:
            for bus in ("/dev/i2c-1", "/dev/i2c-0"):
                try:
                    fd = os.open(bus, os.O_RDWR)
                    fcntl.ioctl(fd, I2C_SLAVE_FORCE, BQ_ADDR)
                    self.i2c = fd
                    if self._word("volt") is None:
                        os.close(fd)
                        self.i2c = None
                        continue
                    self.bus = bus
                    break
                except OSError:
                    continue
        self.source = (f"sysfs {self.ps}" if self.ps else
                       f"i2c {self.bus} 0x55" if self.i2c is not None else "なし")

    def _word(self, key, signed=False):
        try:
            os.write(self.i2c, bytes([BQ[key]]))
            b = os.read(self.i2c, 2)
            v = b[0] | (b[1] << 8)
            if signed and v >= 0x8000:
                v -= 0x10000
            return v
        except OSError:
            return None

    def read(self):
        if self.ps:
            def num(name, k=1e6):
                s = open_text(self.ps + "/" + name)
                try:
                    return int(s) / k
                except (TypeError, ValueError):
                    return None
            v, i = num("voltage_now"), num("current_now")
            p = num("power_avg")
            st = open_text(self.ps + "/status") or ""
            soc = num("capacity", 1)
            tte = num("time_to_empty_avg", 60) or num("time_to_empty_now", 60)
            temp = num("temp", 10)
            if i is not None and st.lower().startswith("discharging") and i > 0:
                i = -i                         # ドライバによって符号の向きが違う
            on_bat = st.lower().startswith("discharging") or (i is not None and i < -0.03)
        elif self.i2c is not None:
            mv, ma = self._word("volt"), self._word("cur", True)
            if mv is None or ma is None:
                return None
            v, i = mv / 1000, ma / 1000
            pw = self._word("avgpow", True)
            p = abs(pw) / 1000 if pw is not None else None
            soc = self._word("soc")
            t = self._word("tte")
            tte = None if t in (None, 0xFFFF) else t
            k = self._word("temp")
            temp = k / 10 - 273.15 if k else None
            flags = self._word("flags") or 0
            on_bat = bool(flags & 0x0001) or i < -0.03     # DSG ビット
        else:
            return None
        if v is None:
            return None
        if on_bat:
            # 実機 (bq27220-0) の power_avg は電圧×電流と大きくずれる (0.15W vs 1.68W) ので、
            # 電圧×電流を優先し、電流が読めない時だけ平均電力を使う
            p = v * abs(i) if i is not None else (abs(p) if p else None)
        else:
            p = None
        return {"v": v, "i": i, "p": p, "soc": soc, "on_bat": on_bat, "tte": tte, "temp": temp}


def open_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# ================================================================ helper 呼び出し
def ctl(*args):
    base = [CTL] if os.geteuid() == 0 else ["sudo", "-n", CTL]
    if os.environ.get("HEXAPOWER_CTL_CMD"):            # テスト用: "python3 path/hexapower_ctl.py"
        base = os.environ["HEXAPOWER_CTL_CMD"].split()
    try:
        p = subprocess.run(base + list(args), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": str(e)}
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        err = (p.stderr or p.stdout).strip().splitlines()
        msg = err[-1] if err else f"exit {p.returncode}"
        if "password" in msg.lower():
            msg = "sudo の許可がありません (install.sh を実行)"
        return {"ok": False, "error": msg}


# ================================================================ app
class App:
    HEX_R = 25
    POS = [(27, 48), (74, 48), (121, 48), (50, 86), (97, 86), (144, 86)]

    def __init__(self, fb, keys, battery, sim=None):
        self.fb, self.keys, self.bat, self.sim = fb, keys, battery, sim
        self.font = find_font(14)
        self.font_s = find_font(12)
        self.font_t = find_font(10)
        self.font_bs = find_font(11, bold=True)
        self.font_big = find_font(28, bold=True)
        self.font_hex = find_font(11, bold=True)
        self.honey = honeycomb_bg()
        self.sel = 0
        self.state = {}
        self.hist = collections.deque(maxlen=HIST_N)     # (t, W or None)
        self.marks = collections.deque(maxlen=20)         # 切替した時刻
        self.last = None
        self.toast = ("", 0.0)
        self.deltas = {}                                  # key → "−0.12W"
        self.pending = None                               # (key, 切替時刻, 切替前の平均)
        self.next_sample = 0.0
        self.refresh_state()

    # ---- data
    def refresh_state(self):
        r = ctl("get")
        if "bt" in r or "cpu" in r:
            self.state = r
        elif r.get("error"):
            self.say(r["error"], 4)

    def sample(self):
        now = time.time()
        if now < self.next_sample:
            return False
        self.next_sample = now + SAMPLE_SEC
        try:
            self.last = self.bat.read()
        except Exception as e:
            print("battery read failed:", e, flush=True)
            self.last = None
        self.hist.append((now, self.last["p"] if self.last else None))
        self.check_delta(now)
        return True

    def avg(self, t0, t1):
        vals = [p for t, p in self.hist if t0 <= t <= t1 and p is not None]
        return sum(vals) / len(vals) if len(vals) >= 3 else None

    def check_delta(self, now):
        if not self.pending:
            return
        key, t_sw, before = self.pending
        if now < t_sw + DELTA_WAIT + DELTA_SPAN:
            return
        self.pending = None
        after = self.avg(t_sw + DELTA_WAIT, now)
        if before is None or after is None:
            self.say("差は測れませんでした (電池で動いている時だけ測れます)", 3)
            return
        d = after - before
        txt = f"{d:+.2f}W"
        self.deltas[key] = txt
        name = dict((k, n) for k, n, _ in ITEMS)[key]
        self.say(f"{name} 切替の差: {txt}  ({before:.2f}→{after:.2f}W)", 4)

    # ---- actions
    def value_of(self, key):
        s = self.state.get(key)
        if key == "cpu":
            return s.get("preset") if isinstance(s, dict) else None
        return s

    def toggle(self, key):
        cur = self.value_of(key)
        if cur is None:
            self.say("この本体では見つかりません", 2.5)
            return
        if key == "boot":
            r = ctl("boot", "off" if cur else "on")
        elif key == "cpu":
            r = ctl("set", "cpu", self.cpu_next(cur))
        else:
            r = ctl("set", key, "off" if cur else "on")
        if not r.get("ok"):
            self.say("失敗: " + str(r.get("error", "?")), 4)
            return
        self.state = r.get("state", self.state)
        if key != "boot":
            now = time.time()
            self.marks.append(now)
            self.pending = (key, now, self.avg(now - 15, now))
            self.say("切り替えました。15秒後に差を出します", 2.5)
        else:
            self.say("起動時にも当てます" if self.state.get("boot") else "起動時には当てません", 2.5)

    def single_core(self):
        c = self.state.get("cpu")
        return isinstance(c, dict) and c.get("total", 4) < 2

    def cpu_next(self, cur):
        """1コアの本体では エコ(2コア) は意味が無いので 標準 ↔ 最小 の2段"""
        if self.single_core():
            return "min" if cur == "std" else "std"
        return CPU_NEXT.get(cur, "std")

    def say(self, msg, sec=2.5):
        self.toast = (msg, time.time() + sec)

    # ---- drawing
    def label_of(self, key):
        """(六角形の下段の文字, 色)"""
        v = self.value_of(key)
        if v is None:
            return "なし", NA
        if key == "cpu":
            return CPU_NAMES.get(v, v), (FG if v == "std" else OFFC)
        if key == "wifips":                       # ON = 節電中
            return ("省電力" if v else "通常"), (OFFC if v else FG)
        if key == "boot":
            return ("当てる" if v else "当てない"), (ACC if v else SUB)
        return ("ON" if v else "OFF"), (FG if v else OFFC)

    def detail_of(self, key):
        v = self.value_of(key)
        if key == "cpu" and isinstance(self.state.get("cpu"), dict):
            c = self.state["cpu"]
            s = f"{c.get('cores', '?')}コア / 上限 {c.get('max', '?')}MHz"
            return s + (f" / 今 {c['cur']}MHz" if c.get("cur") else "")
        if v is None:
            return "この本体では見つかりません"
        if key == "cpu":
            return "標準↔最小(最低クロック)" if self.single_core() else dict((k, d) for k, _, d in ITEMS)[key]
        return dict((k, d) for k, _, d in ITEMS)[key]

    def draw(self):
        img = self.honey.copy()
        d = ImageDraw.Draw(img)
        # ---- 六角形
        for i, (key, name, _) in enumerate(ITEMS):
            cx, cy = self.POS[i]
            r = self.HEX_R
            on = i == self.sel
            pts = hexpts(cx, cy, r)
            d.polygon(pts, fill=(18, 30, 40) if on else (18, 21, 29))
            if on:
                glow_hex(img, cx, cy, r, ACC)
                d = ImageDraw.Draw(img)
            else:
                d.polygon(pts, outline=(44, 50, 64), width=2)
            st, col = self.label_of(key)
            f = self.font_hex
            d.text((cx - f.getlength(name) / 2, cy - 15), name, fill=FG if on else SUB, font=f)
            ft = self.font_t
            d.text((cx - ft.getlength(st) / 2, cy + 1), st, fill=col, font=ft)
        # ---- 選択中の説明 (六角形の下)
        key, name, _ = ITEMS[self.sel]
        st, col = self.label_of(key)
        x, y = 4, 115
        d.text((x, y), f"{self.sel + 1} {name}", fill=FG, font=self.font_bs)
        x2 = x + self.font_bs.getlength(f"{self.sel + 1} {name}") + 6
        d.text((x2, y + 1), st, fill=col, font=self.font_s)
        if key in self.deltas:
            dl = "前回の差 " + self.deltas[key]
            d.text((168 - self.font_t.getlength(dl), y + 2), dl, fill=ACC, font=self.font_t)
        self.text_fit(d, (x, y + 18), self.detail_of(key), 164, self.font_t, SUB)
        # ---- 右: 消費電力
        self.draw_power(img, d)
        return self.chrome(img)

    def text_fit(self, d, xy, text, w, font, col):
        if font.getlength(text) > w:
            while text and font.getlength(text + "…") > w:
                text = text[:-1]
            text += "…"
        d.text(xy, text, fill=col, font=font)

    def draw_power(self, img, d):
        x0, x1 = 172, LCD_W - 4
        d.rounded_rectangle((x0 - 4, TOP_H + 3, x1 + 1, LCD_H - BOT_H - 3), 6, fill=(16, 19, 27),
                            outline=(34, 40, 52))
        b = self.last
        y = TOP_H + 6
        if b is None:
            d.text((x0, y), "電池の値が読めません", fill=OFFC, font=self.font_s)
            d.text((x0, y + 18), f"読み方: {self.bat.source}", fill=SUB, font=self.font_t)
            d.text((x0, y + 32), "hexapower --probe で確認", fill=SUB, font=self.font_t)
            return
        if b["p"] is not None:
            d.text((x0, y), "消費電力", fill=SUB, font=self.font_t)
            w = f"{b['p']:.2f}"
            d.text((x0, y + 9), w, fill=ACC, font=self.font_big)
            d.text((x0 + self.font_big.getlength(w) + 4, y + 23), "W", fill=ACC, font=self.font_s)
        else:
            d.text((x0, y), "外部給電中", fill=FG, font=self.font_bs)
            d.text((x0, y + 15), "本体の消費は電池で", fill=SUB, font=self.font_t)
            d.text((x0, y + 27), "動いている時だけ測れます", fill=SUB, font=self.font_t)
        line = f"{b['v']:.2f}V"
        if b["i"] is not None:
            line += f"  {b['i'] * 1000:+.0f}mA"
        if b.get("temp"):
            line += f"  {b['temp']:.0f}℃"
        d.text((x0, y + 42), line, fill=SUB, font=self.font_t)
        # グラフ
        gx0, gy0, gx1, gy1 = x0, y + 56, x1 - 2, y + 96
        d.rectangle((gx0, gy0, gx1, gy1), outline=(40, 46, 60))
        pts = [(t, p) for t, p in self.hist if p is not None]
        top = max([3.0] + [p for _, p in pts]) if pts else 3.0
        top = math.ceil(top)
        for k in range(1, top):
            yy = gy1 - (gy1 - gy0) * k / top
            d.line((gx0 + 1, yy, gx1 - 1, yy), fill=(26, 31, 42))
        now = time.time()
        span = HIST_N * SAMPLE_SEC

        def px(t):
            return gx1 - (now - t) / span * (gx1 - gx0)
        for t in self.marks:
            if now - t < span:
                d.line((px(t), gy0 + 1, px(t), gy1 - 1), fill=(90, 70, 30))
        seg = []
        for t, p in self.hist:
            if p is None:
                if len(seg) > 1:
                    d.line(seg, fill=ACC, width=2)
                seg = []
                continue
            seg.append((px(t), gy1 - (gy1 - gy0) * min(p, top) / top))
        if len(seg) > 1:
            d.line(seg, fill=ACC, width=2)
        d.text((gx0 + 2, gy0 + 1), f"{top}W", fill=(80, 88, 104), font=self.font_t)
        d.text((gx1 - self.font_t.getlength("2分半") - 2, gy1 - 12), "2分半", fill=(80, 88, 104),
               font=self.font_t)
        # 残り
        rest = ""
        if b["p"]:
            a = self.avg(now - 30, now) or b["p"]
            if b.get("tte"):
                rest = f"残り 約{b['tte'] / 60:.1f}時間"
            elif b.get("soc") is not None:
                rest = f"残り 約{6.5 * b['soc'] / 100 / a:.1f}時間 (推定)"
        elif b.get("soc") is not None:
            rest = f"電池 {b['soc']:.0f}%"
        d.text((x0, gy1 + 4), rest, fill=FG, font=self.font_t)

    def chrome(self, img):
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, LCD_W, TOP_H - 1), fill=(0, 0, 0))
        d.text((5, 1), "HEXA POWER", fill=ACC, font=self.font_bs)
        b = self.last
        if b:
            st = ("電池" if b["on_bat"] else "外部給電") + (f"  {b['soc']:.0f}%" if b.get("soc") is not None else "")
            d.text((LCD_W - 4 - self.font_t.getlength(st), 3), st, fill=SUB, font=self.font_t)
        d.rectangle((0, LCD_H - BOT_H, LCD_W, LCD_H), fill=(0, 0, 0))
        d.text((4, LCD_H - BOT_H + 1), "FXZC:選ぶ  1-6:直接  ENTER:切替  ESC:終了",
               fill=(210, 210, 210), font=self.font_s)
        msg, until = self.toast
        if msg and time.time() < until:
            f = self.font_t
            w = min(int(f.getlength(msg)) + 16, LCD_W - 8)
            yy = LCD_H - BOT_H - 20
            d.rounded_rectangle(((LCD_W - w) // 2, yy, (LCD_W + w) // 2, yy + 17), 7,
                                fill=(10, 60, 90), outline=ACC)
            self.text_fit(d, ((LCD_W - w) // 2 + 8, yy + 2), msg, w - 14, f, (255, 255, 255))
        return img

    def confirm_quit(self, base):
        img = Image.blend(base, Image.new("RGB", base.size), 0.65)
        d = ImageDraw.Draw(img)
        x0, y0, x1, y1 = 40, 42, LCD_W - 40, LCD_H - 38
        d.rounded_rectangle((x0, y0, x1, y1), 8, fill=(26, 30, 38), outline=ACC, width=2)
        for i, (txt, font, col) in enumerate([("HEXA POWER を終了しますか?", self.font, FG),
                                              ("設定はそのまま残ります", self.font_t, SUB),
                                              ("ENTER: 終了    ESC: 戻る", self.font_s, ACC)]):
            d.text(((LCD_W - font.getlength(txt)) / 2, y0 + 12 + i * 22), txt, fill=col, font=font)
        self.fb.show(img)
        self.keys.flush(0.2)
        while True:
            for kind, val in self.keys.poll(0.5):
                self.keys.flush(0.1)
                return kind == "ACT" and val == "ENTER"

    # ---- loop
    def move(self, act):
        row, col = divmod(self.sel, 3)
        if act == "LEFT":
            col = (col - 1) % 3
        elif act == "RIGHT":
            col = (col + 1) % 3
        elif act in ("UP", "DOWN"):
            row = 1 - row
        elif act == "TAB":
            self.sel = (self.sel + 1) % 6
            return
        self.sel = row * 3 + col

    def run(self):
        self.keys.flush(0.4)
        dirty = True
        next_state = time.time() + 10
        while True:
            if self.sample():
                dirty = True
            if time.time() > next_state:        # CPU の今のクロックなどを取り直す
                self.refresh_state()
                next_state = time.time() + 10
                dirty = True
            if dirty or self.toast[1] > time.time() - 0.5:
                frame = self.draw()
                self.fb.show(frame)
                dirty = False
            for kind, val in self.keys.poll(0.2):
                dirty = True
                if kind == "ACT" and val == "ESC":
                    if self.confirm_quit(self.draw()):
                        return "ESC"
                elif kind == "ACT" and val == "ENTER":
                    self.toggle(ITEMS[self.sel][0])
                elif kind == "ACT":
                    self.move(val)
                elif kind == "CHAR" and val in "123456":
                    self.sel = int(val) - 1
                elif kind == "CHAR" and val == "r":
                    self.refresh_state()
                elif kind == "CHAR" and val == "q":
                    return "q"


# ================================================================ launcher (HEXA CLIP と同じ)
def launcher_pids():
    pids = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            cmd = open(f"/proc/{d}/cmdline", "rb").read()
        except OSError:
            continue
        if b"M5CardputerZero-APPLaunch" in cmd:
            pids.append(int(d))
    return pids


def signal_launcher(sig):
    for pid in launcher_pids():
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


# ================================================================ PC 用シミュレーション
class SimFB:
    def __init__(self, outdir):
        self.outdir, self.n, self.img = outdir, 0, None
        os.makedirs(outdir, exist_ok=True)

    def show(self, img):
        self.img = img.copy()

    def snap(self, name):
        self.img.resize((LCD_W * 2, LCD_H * 2), Image.NEAREST).save(os.path.join(self.outdir, name))

    def clear(self):
        pass


class SimKeys:
    """台本どおりにキーを押す: [(待つ秒, ('ACT','ENTER')) or (待つ秒, ('SNAP','name.png'))]"""

    def __init__(self, script, fb):
        self.script, self.fb, self.t = list(script), fb, time.time()

    def poll(self, timeout=0.0):
        time.sleep(min(timeout, 0.05))
        out = []
        while self.script and time.time() - self.t >= self.script[0][0]:
            _, ev = self.script.pop(0)
            self.t = time.time()
            if ev[0] == "SNAP":
                if self.fb.img is not None:
                    self.fb.snap(ev[1])
            else:
                out.append(ev)
        if not self.script and not out:
            out.append(("CHAR", "q"))
        return out

    def flush(self, sec=0):
        pass


class SimBattery:
    """設定によって消費が変わる偽の電池"""
    source = "sim"

    def __init__(self, app_ref, on_bat=True):
        self.app_ref, self.on_bat, self.soc = app_ref, on_bat, 78.0

    def read(self):
        app = self.app_ref[0]
        st = app.state if app else {}
        p = 2.05
        p += 0.06 if st.get("bt") else 0
        p += 0.12 if st.get("hdmi") else 0
        p += 0.28 if st.get("lan") else 0
        p += {"std": 0.35, "eco": 0.18, "min": 0.05}.get((st.get("cpu") or {}).get("preset"), 0.35)
        p += 0.0 if st.get("wifips") else 0.2
        p += random.uniform(-0.05, 0.05)
        v = 3.86
        if not self.on_bat:
            return {"v": 4.12, "i": 0.35, "p": None, "soc": self.soc, "on_bat": False, "tte": None, "temp": 31.0}
        return {"v": v, "i": -p / v, "p": p, "soc": self.soc, "on_bat": True, "tte": None, "temp": 33.0}


def run_sim(outdir):
    global DELTA_WAIT, DELTA_SPAN, SAMPLE_SEC
    DELTA_WAIT, DELTA_SPAN, SAMPLE_SEC = 0.5, 1.0, 0.1
    fb = SimFB(outdir)
    ref = [None]
    bat = SimBattery(ref)
    A, C = (lambda v: ("ACT", v)), (lambda v: ("CHAR", v))
    script = [(2.0, ("SNAP", "01_home.png")),
              (0.1, A("ENTER")), (2.2, ("SNAP", "02_bt_off_delta.png")),
              (0.1, C("3")), (0.1, A("ENTER")), (2.2, ("SNAP", "03_lan_off.png")),
              (0.1, C("4")), (0.1, A("ENTER")), (0.3, A("ENTER")), (2.2, ("SNAP", "04_cpu_min.png")),
              (0.1, C("5")), (0.1, A("ENTER")), (2.2, ("SNAP", "05_wifips.png")),
              (0.1, C("6")), (0.1, A("ENTER")), (0.3, ("SNAP", "06_boot.png"))]
    keys = SimKeys(script, fb)
    app = App(fb, keys, bat)
    ref[0] = app
    app.run()
    # 外部給電中の見た目
    bat.on_bat = False
    app.hist.clear()
    app.next_sample = 0
    app.sample()
    app.sel = 1
    fb.show(app.draw())
    fb.snap("07_external.png")
    print("sim done:", outdir)


# ================================================================ main
def probe():
    b = Battery()
    print("電池の読み方:", b.source)
    try:
        print("値:", b.read())
    except Exception as e:
        print("読み取り失敗:", e)
    print("helper:", json.dumps(ctl("get"), ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--sim", metavar="OUTDIR")
    ap.add_argument("--no-pause", action="store_true", help="ランチャーを一時停止しない")
    ap.add_argument("--check-font", action="store_true", help="使う日本語フォントを表示 (無ければ終了コード1)")
    a = ap.parse_args()
    if a.check_font:
        find_font(12)
        print("font:", _FONT_FILE.get(False) or "見つかりません")
        raise SystemExit(0 if _FONT_FILE.get(False) else 1)
    if a.probe:
        return probe()
    if a.sim:
        return run_sim(a.sim)
    fb = FrameBuffer()
    keys = Keys(find_keyboard())
    paused = False
    if not a.no_pause and launcher_pids():
        signal_launcher(signal.SIGSTOP)
        paused = True
        for sg in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sg, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    reason = "unknown"
    try:
        print(f"HEXA POWER {VERSION}", flush=True)
        reason = App(fb, keys, Battery()).run()
    except KeyboardInterrupt:
        reason = "Ctrl+C"
    except Exception:
        import traceback
        traceback.print_exc()
        reason = "error"
    finally:
        print("exit:", reason, flush=True)
        keys.close()
        fb.clear()
        if paused:
            signal_launcher(signal.SIGCONT)


if __name__ == "__main__":
    main()
