#!/bin/bash
# Режим настройки: выключить корень только для чтения, чтобы изменения на плате
# сохранялись на карту. Wi-Fi и SSH работают в обоих режимах. Нужна перезагрузка.
# Обратно: usbc-mode-production.sh. После правок — sync.
set -euo pipefail
B=/boot/firmware

mount -o remount,rw "$B"
trap 'sync; mount -o remount,ro "$B"' EXIT
sed -i "s/overlayroot=tmpfs //" "$B/cmdline.txt"
# initramfs без overlay не нужен и добавляет секунды к загрузке
sed -i "s/^auto_initramfs=1$/auto_initramfs=0/" "$B/config.txt"
echo "cmdline: $(cat "$B/cmdline.txt")"
echo "Режим настройки включится после перезагрузки: sudo reboot"
