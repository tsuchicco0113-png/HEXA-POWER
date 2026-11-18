#!/usr/bin/env python3
"""HEXA POWER ヘルパー (root で動く部分だけ)

/usr/local/sbin/hexapower-ctl にコピーされ、sudoers でこのファイルだけ
パスワードなしで実行できるようにする (install.sh)。決まった操作しかしない。

  hexapower-ctl get                      今の状態を JSON で
  hexapower-ctl set bt on|off            Bluetooth (rfkill)
  hexapower-ctl set hdmi on|off          HDMI 出力
  hexapower-ctl set lan on|off           有線LAN チップ (USB の SR9900 を切り離す)
  hexapower-ctl set cpu std|eco|min      CPU (標準 / 2コア / 2コア+最低クロック)
  hexapower-ctl set wifips on|off        Wi-Fi の省電力
  hexapower-ctl set bl 10〜100           画面の明るさ (%)
  hexapower-ctl boot on|off              起動時に今の設定を自動で当てる
  hexapower-ctl apply-boot               (systemd から) 保存した設定を当てる

テスト用: 環境変数 HEXAPOWER_ROOT を付けると /sys などをその下の偽物に向ける。
"""
import glob
import json
import os
import shutil
import subprocess
import sys

ROOT = os.environ.get("HEXAPOWER_ROOT", "")
CONF = ROOT + "/etc/hexapower.json"
SYS = ROOT + "/sys"
LAN_VIDS = {"0fe6"}                      # CoreChips (SR9900 は 0fe6:9900)

CPU_PRESETS = {
    "std": {"cores": 4, "freq": "max"},
    "eco": {"cores": 2, "freq": "max"},
    "min": {"cores": 2, "freq": "min"},
}


# ------------------------------------------------------------ small io
def rd(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def wr(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def load_conf():
    try:
        with open(CONF) as f:
            c = json.load(f)
            return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def save_conf(c):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=1)
    os.replace(tmp, CONF)


def run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return p.returncode, p.stdout + p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


# ------------------------------------------------------------ Bluetooth
def bt_nodes():
    return [os.path.dirname(t) for t in glob.glob(SYS + "/class/rfkill/rfkill*/type")
            if rd(t) == "bluetooth"]


def bt_get():
    nodes = bt_nodes()
    if not nodes:
        return None
    return all(rd(n + "/soft") == "0" for n in nodes)


def bt_set(on):
    nodes = bt_nodes()
    if not nodes:
        raise RuntimeError("Bluetooth が見つかりません")
    for n in nodes:
        wr(n + "/soft", 0 if on else 1)


# ------------------------------------------------------------ HDMI
def hdmi_nodes():
    return sorted(glob.glob(SYS + "/class/drm/card*-HDMI-A-*"))


def hdmi_get():
    nodes = hdmi_nodes()
    if nodes:
        return load_conf().get("hdmi_forced_off") is not True
    if ROOT == "" and shutil.which("vcgencmd"):
        rc, out = run(["vcgencmd", "display_power", "-1", "2"])
        if rc == 0 and "=" in out:
            return out.strip().endswith("1")
    return None


def hdmi_set(on):
    nodes = hdmi_nodes()
    if nodes:
        for n in nodes:
            wr(n + "/status", "detect" if on else "off")
        c = load_conf()
        c["hdmi_forced_off"] = not on
        save_conf(c)
        return
    if ROOT == "" and shutil.which("vcgencmd"):
        rc, out = run(["vcgencmd", "display_power", "1" if on else "0", "2"])
        if rc == 0:
            return
        raise RuntimeError("vcgencmd: " + out.strip())
    raise RuntimeError("HDMI の出力が見つかりません")


# ------------------------------------------------------------ LAN (USB の SR9900)
def lan_find():
    """有線LANの USB デバイスのディレクトリ。切り離し中でも見つかるよう conf にも覚える"""
    for d in glob.glob(SYS + "/bus/usb/devices/*"):
        if rd(d + "/idVendor") in LAN_VIDS:
            return os.path.realpath(d)
    for n in glob.glob(SYS + "/class/net/*"):
        name = os.path.basename(n)
        if name == "lo" or name.startswith(("wlan", "p2p")):
            continue
        dev = os.path.realpath(n + "/device")
        if "/usb" in dev:                     # USB インターフェース → 親が USB デバイス
            parent = os.path.dirname(dev)
            if os.path.exists(parent + "/authorized"):
                return parent
    saved = load_conf().get("lan_dev")
    if saved and os.path.exists(ROOT + saved + "/authorized"):
        return ROOT + saved
    return None


def lan_get():
    d = lan_find()
    if not d:
        return None
    return rd(d + "/authorized") == "1"


def lan_set(on):
    d = lan_find()
    if not d:
        raise RuntimeError("有線LANのチップが見つかりません")
    c = load_conf()
    c["lan_dev"] = d[len(ROOT):]
    save_conf(c)
    if not on:
        for n in glob.glob(d + "/*/net/*"):
            if ROOT == "":
                run(["ip", "link", "set", "dev", os.path.basename(n), "down"])
    wr(d + "/authorized", 1 if on else 0)


# ------------------------------------------------------------ CPU
def cpu_cores_online():
    n = 1                                     # cpu0 は止められない
    for p in sorted(glob.glob(SYS + "/devices/system/cpu/cpu[1-9]*/online")):
        n += rd(p) == "1"
    return n


def cpu_policy():
    for p in (SYS + "/devices/system/cpu/cpufreq/policy0", SYS + "/devices/system/cpu/cpu0/cpufreq"):
        if os.path.exists(p + "/scaling_max_freq"):
            return p
    return None


def cpu_get():
    pol = cpu_policy()
    info = {"cores": cpu_cores_online(),
            "total": 1 + len(glob.glob(SYS + "/devices/system/cpu/cpu[1-9]*/online"))}
    if pol:
        info.update({k: int(rd(f"{pol}/{f}", "0")) // 1000 for k, f in
                     (("max", "scaling_max_freq"), ("hwmax", "cpuinfo_max_freq"),
                      ("hwmin", "cpuinfo_min_freq"), ("cur", "scaling_cur_freq"))})
        info["gov"] = rd(pol + "/scaling_governor", "")
    preset = "std"
    names = ["std", "min"] if info["total"] < 2 else list(CPU_PRESETS)   # 1コアの本体
    for name in names:
        p = CPU_PRESETS[name]
        want_max = info.get("hwmax") if p["freq"] == "max" else info.get("hwmin")
        if min(p["cores"], info["total"]) == info["cores"] and (not pol or info.get("max") == want_max):
            preset = name
            break
    else:
        preset = "custom"
    info["preset"] = preset
    return info


def cpu_set(name):
    if name not in CPU_PRESETS:
        raise RuntimeError("CPU の設定は std / eco / min")
    p = CPU_PRESETS[name]
    cores = sorted(glob.glob(SYS + "/devices/system/cpu/cpu[1-9]*/online"))
    for i, path in enumerate(cores, start=1):
        wr(path, 1 if i < p["cores"] else 0)
    pol = cpu_policy()
    if pol:
        f = rd(pol + ("/cpuinfo_max_freq" if p["freq"] == "max" else "/cpuinfo_min_freq"))
        if f:
            wr(pol + "/scaling_max_freq", f)


# ------------------------------------------------------------ Wi-Fi 省電力
def wifi_iface():
    for n in sorted(glob.glob(SYS + "/class/net/*")):
        if os.path.exists(n + "/wireless") or os.path.exists(n + "/phy80211"):
            return os.path.basename(n)
    return None


def wifips_get():
    ifc = wifi_iface()
    if not ifc:
        return None
    if ROOT:
        return load_conf().get("wifips_sim", True)
    if not shutil.which("iw"):
        return None
    rc, out = run(["iw", "dev", ifc, "get", "power_save"])
    if rc != 0:
        return None
    return "on" in out.lower()


def wifips_set(on):
    ifc = wifi_iface()
    if not ifc:
        raise RuntimeError("Wi-Fi が見つかりません")
    if ROOT:
        c = load_conf()
        c["wifips_sim"] = on
        save_conf(c)
        return
    rc, out = run(["iw", "dev", ifc, "set", "power_save", "on" if on else "off"])
    if rc != 0:
        raise RuntimeError("iw: " + out.strip())


# ------------------------------------------------------------ 画面の明るさ
def bl_node():
    nodes = sorted(glob.glob(SYS + "/class/backlight/*"))
    return nodes[0] if nodes else None


def bl_get():
    n = bl_node()
    if not n:
        return None
    try:
        mx = int(rd(n + "/max_brightness", "0"))
        cur = int(rd(n + "/brightness", "0"))
    except ValueError:
        return None
    return round(cur * 100 / mx) if mx else None


def bl_set(pct):
    n = bl_node()
    if not n:
        raise RuntimeError("バックライトが見つかりません")
    pct = max(5, min(100, int(pct)))         # 真っ暗にはしない
    mx = int(rd(n + "/max_brightness", "100"))
    wr(n + "/brightness", max(1, round(mx * pct / 100)))


# ------------------------------------------------------------ まとめ
SETTERS = {"bt": bt_set, "hdmi": hdmi_set, "lan": lan_set, "wifips": wifips_set}


def state():
    c = load_conf()
    return {"bt": bt_get(), "hdmi": hdmi_get(), "lan": lan_get(), "cpu": cpu_get(),
            "wifips": wifips_get(), "bl": bl_get(), "boot": bool(c.get("boot")), "saved": c.get("want", {})}


def remember(key, value):
    c = load_conf()
    c.setdefault("want", {})[key] = value
    save_conf(c)


def apply_boot():
    c = load_conf()
    if not c.get("boot"):
        print("boot apply: off")
        return
    for key, value in c.get("want", {}).items():
        try:
            if key == "cpu":
                cpu_set(value)
            elif key == "bl":
                bl_set(value)
            elif key in SETTERS:
                SETTERS[key](value == "on")
            print("boot apply:", key, value)
        except Exception as e:                # 1つ失敗しても残りは当てる
            print("boot apply failed:", key, e)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    try:
        if cmd == "get":
            print(json.dumps(state(), ensure_ascii=False))
        elif cmd == "set" and len(argv) == 4:
            key, val = argv[2], argv[3]
            if key == "cpu":
                cpu_set(val)
            elif key == "bl" and val.isdigit():
                bl_set(val)
            elif key in SETTERS and val in ("on", "off"):
                SETTERS[key](val == "on")
            else:
                raise RuntimeError("不明な設定: " + " ".join(argv[2:]))
            remember(key, val)
            print(json.dumps({"ok": True, "state": state()}, ensure_ascii=False))
        elif cmd == "boot" and len(argv) == 3 and argv[2] in ("on", "off"):
            c = load_conf()
            c["boot"] = argv[2] == "on"
            save_conf(c)
            print(json.dumps({"ok": True, "state": state()}, ensure_ascii=False))
        elif cmd == "apply-boot":
            apply_boot()
        else:
            print(__doc__)
            return 2
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
