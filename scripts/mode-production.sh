#!/bin/bash
# Рабочий режим: корень только для чтения (overlayroot=tmpfs). Все записи идут в RAM и
# пропадают при перезагрузке — выдёргивание питания ничего не портит. Wi-Fi и SSH работают.
# Обратно: usbc-mode-setup.sh. Нужна перезагрузка.
set -euo pipefail
B=/boot/firmware

dpkg -s overlayroot >/dev/null 2>&1 || { echo "нет пакета overlayroot (apt install overlayroot, при выключенном overlay)"; exit 1; }
mount -o remount,rw "$B"
trap 'sync; mount -o remount,ro "$B"' EXIT
# overlayroot работает из initramfs
sed -i "s/^auto_initramfs=0$/auto_initramfs=1/" "$B/config.txt"
grep -q "overlayroot=tmpfs" "$B/cmdline.txt" || sed -i "s/^/overlayroot=tmpfs /" "$B/cmdline.txt"
# fsck в initramfs не нужен: нижний слой всегда смонтирован только для чтения
grep -qw "fastboot" "$B/cmdline.txt" || sed -i "s/$/ fastboot/" "$B/cmdline.txt"
echo "cmdline: $(cat "$B/cmdline.txt")"
echo "Рабочий режим включится после перезагрузки: sudo reboot"
