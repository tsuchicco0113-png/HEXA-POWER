#!/bin/sh
# HEXA POWER installer for CardputerZero
# 使い方: cd ~/hexapower && sh install.sh   (sudo は付けずに実行)
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
U=$(id -un)
if [ "$U" = "root" ]; then
  echo "sudo を付けずに実行してください (sh install.sh)"; exit 1
fi
APPL=/usr/share/APPLaunch

echo "== packages"
sudo apt-get install -y python3-pil python3-numpy iw rfkill >/dev/null

echo "== files"
chmod +x "$DIR/hexapower.py" "$DIR/hexapower"
sudo ln -sf "$DIR/hexapower" /usr/local/bin/hexapower
# root で動くヘルパーは root 所有の場所へコピー (ユーザーが書き換えられないように)
sudo install -o root -g root -m 755 "$DIR/hexapower_ctl.py" /usr/local/sbin/hexapower-ctl

echo "== sudo の許可 (ヘルパーだけパスワードなし)"
TMP=$(mktemp)
echo "$U ALL=(root) NOPASSWD: /usr/local/sbin/hexapower-ctl" > "$TMP"
if sudo visudo -cf "$TMP" >/dev/null; then
  sudo install -o root -g root -m 440 "$TMP" /etc/sudoers.d/hexapower
else
  echo "   sudoers の確認に失敗したので追加しませんでした"
fi
rm -f "$TMP"

echo "== 起動時に設定を当てるサービス"
sudo tee /etc/systemd/system/hexapower.service >/dev/null <<UNIT
[Unit]
Description=HEXA POWER (起動時に節電設定を当てる)
After=multi-user.target bluetooth.service

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 15
ExecStart=/usr/local/sbin/hexapower-ctl apply-boot

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable hexapower >/dev/null 2>&1

echo "== 日本語フォント"
if ! python3 "$DIR/hexapower.py" --check-font; then
  echo "   字がそろったフォントが無いので fonts-noto-cjk を入れます"
  sudo apt-get install -y fonts-noto-cjk >/dev/null
fi

echo "== permissions"
NEED_REBOOT=0
for g in video input i2c; do
  if getent group "$g" >/dev/null && ! id -nG "$U" | tr ' ' '\n' | grep -qx "$g"; then
    sudo usermod -aG "$g" "$U"
    NEED_REBOOT=1
  fi
done

if [ -d "$APPL/applications" ]; then
  echo "== launcher (APPLaunch)"
  REF="$APPL/share/images/camera1.png"
  [ -f "$REF" ] || REF=64
  python3 "$DIR/make_icon.py" /tmp/hexapower.png "$REF"
  sudo install -m 644 /tmp/hexapower.png "$APPL/share/images/hexapower.png"
  sudo tee "$APPL/applications/hexapower.desktop" >/dev/null <<DESK
[Desktop Entry]
Type=Application
Name=HEXA POWER
Comment=Power monitor + power saving switches
Exec=/usr/local/bin/hexapower
Icon=share/images/hexapower.png
Terminal=false
Categories=Utility;
StartupNotify=false
DESK
  NEED_REBOOT=1
fi

echo "== done"
/usr/local/bin/hexapower --probe || true
if [ "$NEED_REBOOT" = 1 ]; then
  echo ""
  echo "*** アイコン追加と権限の反映のため、再起動してください: sudo reboot"
fi
