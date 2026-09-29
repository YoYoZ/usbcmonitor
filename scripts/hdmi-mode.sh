#!/bin/bash
# Переключение HDMI-режима через video= в cmdline.txt. Нужна перезагрузка.
#   auto     — 1080p60, если монитор его заявляет в EDID (CEA), иначе родной режим монитора (по умолчанию)
#   edid     — только EDID монитора, без предпочтений
#   1080p50  — 1080p50 из EDID, если есть (лучше всего для 25 fps: каждый кадр ровно 2 раза)
#   force1080p60 / force1080p50 / force720p60 — принудительно, даже без EDID (CVT-тайминги;
#                                               полевые мониторы могут их не принять)
set -euo pipefail

CMDLINE=/boot/firmware/cmdline.txt
[ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt

case "${1:-}" in
  auto)          NEW="video=HDMI-A-1:1920x1080@60" ;;
  edid)          NEW="" ;;
  1080p50)       NEW="video=HDMI-A-1:1920x1080@50" ;;
  force1080p60)  NEW="video=HDMI-A-1:1920x1080R@60D" ;;
  force1080p50)  NEW="video=HDMI-A-1:1920x1080R@50D" ;;
  force720p60)   NEW="video=HDMI-A-1:1280x720@60D" ;;
  *)
    echo "usage: $0 auto|edid|1080p50|force1080p60|force1080p50|force720p60"
    echo "текущий: $(grep -o 'video=HDMI-A-1:[^ ]*' "$CMDLINE" || echo '(нет, только EDID)')"
    exit 1 ;;
esac

# /boot/firmware смонтирован только для чтения (см. mode-production.sh)
BOOTMNT="$(dirname "$CMDLINE")"
mount -o remount,rw "$BOOTMNT"
trap 'sync; mount -o remount,ro "$BOOTMNT"' EXIT

cp "$CMDLINE" "$CMDLINE.bak-hdmi-mode"
# убрать старый video=HDMI-A-1:..., дописать новый; файл остаётся одной строкой
LINE="$(sed -E 's/ ?video=HDMI-A-1:[^ ]*//g' "$CMDLINE" | tr -d '\n')"
[ -n "$NEW" ] && LINE="$LINE $NEW"
printf '%s\n' "$LINE" > "$CMDLINE"

echo "cmdline.txt: $(cat "$CMDLINE")"
echo "Перезагрузите: sudo reboot"
