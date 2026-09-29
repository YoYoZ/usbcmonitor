#!/bin/bash
# Собирает диагностику в один лог-файл: usbc-diagnose-<timestamp>.log
# в текущей директории (или в $1, если передан).
#
# Использование:
#   sudo /usr/local/sbin/usbc-diagnose.sh [/путь/для/лога] [/dev/v4l/by-id/камера]
set -uo pipefail

OUTDIR="${1:-/var/log}"
CAMERA_DEV="${2:-/dev/v4l/by-id/blackmagic-uvc-video-index0}"
DECODER_DEV="/dev/video10"
TS="$(date '+%Y%m%d-%H%M%S')"
LOG="${OUTDIR}/usbc-diagnose-${TS}.log"

section() {
  {
    echo
    echo "===== $1 ====="
  } >>"$LOG"
}

echo "Логирую в $LOG"
: >"$LOG"

section "lsusb -t"
lsusb -t >>"$LOG" 2>&1

section "lsusb -v (краткая выжимка идентификаторов устройств)"
lsusb >>"$LOG" 2>&1

section "dmesg | grep -i uvcvideo"
dmesg | grep -i uvcvideo >>"$LOG" 2>&1

section "dmesg | grep -i bcm2835-codec"
dmesg | grep -i bcm2835-codec >>"$LOG" 2>&1

section "dmesg | grep -i -E 'rndis|cdc_ether|cdc_acm' (composite-интерфейсы камеры)"
dmesg | grep -i -E "rndis|cdc_ether|cdc_acm" >>"$LOG" 2>&1

section "v4l2-ctl --list-devices"
v4l2-ctl --list-devices >>"$LOG" 2>&1

section "/dev/v4l/by-id и by-path"
ls -l /dev/v4l/by-id/ >>"$LOG" 2>&1
ls -l /dev/v4l/by-path/ >>"$LOG" 2>&1

section "v4l2-ctl --list-formats-ext на всех /dev/video*"
for dev in /dev/video*; do
  [ -e "$dev" ] || continue
  echo "--- $dev ---" >>"$LOG"
  v4l2-ctl -d "$dev" --list-formats-ext >>"$LOG" 2>&1
  echo "--- $dev (out formats, если M2M) ---" >>"$LOG"
  v4l2-ctl -d "$dev" --list-formats-out >>"$LOG" 2>&1
done

section "версии пакетов"
{
  dpkg -l | grep -E "gstreamer1.0|v4l-utils|libraspberrypi|linux-image|raspberrypi-kernel|mpv|ffmpeg"
  echo "---"
  gst-launch-1.0 --version
  echo "---"
  uname -a
  echo "---"
  cat /etc/os-release
  echo "---"
  BOOTDIR=/boot/firmware; [ -f "$BOOTDIR/config.txt" ] || BOOTDIR=/boot
  grep -v '^#' "$BOOTDIR/config.txt" | grep -v '^$'
  echo "---"
  cat /proc/cmdline
} >>"$LOG" 2>&1

section "загрузка CPU и температура (снимок)"
{
  vcgencmd measure_temp
  vcgencmd get_throttled
  top -bn1 | head -n 15
  free -h
} >>"$LOG" 2>&1

section "systemd: статус usbc-hdmi-bridge и последние логи"
{
  systemctl status usbc-hdmi-bridge.service --no-pager -l
  echo "---"
  journalctl -u usbc-hdmi-bridge.service -n 100 --no-pager
} >>"$LOG" 2>&1

section "тестовый запуск пайплайна с GST_DEBUG на 20 секунд"
if [ -e "$CAMERA_DEV" ]; then
  # камера и декодер должны быть свободны — сервис на время теста останавливаем
  WAS_ACTIVE=0
  systemctl is-active --quiet usbc-hdmi-bridge.service && WAS_ACTIVE=1 && systemctl stop usbc-hdmi-bridge.service
  {
    echo "камера: $CAMERA_DEV, декодер: $DECODER_DEV"
    # fakevideosink, а не fakesink: у fakesink нет video meta, и GStreamer копирует
    # каждый кадр 3 МБ процессором — fps и CPU получаются не как с kmssink
    GST_DEBUG=3,v4l2*:5 timeout 20 gst-launch-1.0 -v \
      v4l2src device="$CAMERA_DEV" io-mode=mmap \
      ! image/jpeg,width=1920,height=1080,framerate=25/1 \
      ! v4l2jpegdec \
      ! fpsdisplaysink video-sink=fakevideosink text-overlay=false sync=false
  } >>"$LOG" 2>&1
  [ "$WAS_ACTIVE" = 1 ] && systemctl start usbc-hdmi-bridge.service
else
  echo "камера не найдена по пути $CAMERA_DEV — тестовый запуск пропущен" >>"$LOG"
fi

section "загрузка CPU и температура (после теста)"
{
  vcgencmd measure_temp
  top -bn1 | head -n 15
} >>"$LOG" 2>&1

echo "Готово: $LOG"
