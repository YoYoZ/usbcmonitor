#!/bin/bash
# Основной цикл моста UVC(Blackmagic) -> HDMI. В штатной работе не завершается:
# пока нет камеры — держит одну долгоживущую заглушку "NO CAMERA",
# появилась камера — гасит заглушку и запускает декод, камера пропала — обратно.
# Restart=always в systemd-юните — страховка сверху, не основной механизм.

set -u

CAMERA_OVERRIDE="${CAMERA_DEV:-}"
CAMERA_DEV=""
WIDTH="${WIDTH:-1920}"
HEIGHT="${HEIGHT:-1080}"
FPS="${FPS:-25}"
SRC_IO_MODE="${SRC_IO_MODE:-mmap}"
DECODER="${DECODER:-hw}"
LOWRES="${LOWRES:-1}"
CAMERA_GRACE="${CAMERA_GRACE:-20}"

log() {
  echo "[usbc-hdmi-bridge] $*"
}

# Камеру ищем в sysfs (vendor 1edb, узел захвата index 0), а не по udev-ссылке в
# /dev/v4l/by-id: при загрузке udev доходит до камеры на ~25 с позже ядра.
find_camera() {
  if [ -n "${CAMERA_OVERRIDE:-}" ]; then
    [ -e "$CAMERA_OVERRIDE" ] && echo "$CAMERA_OVERRIDE"
    return
  fi
  local d
  for d in /sys/class/video4linux/video*; do
    [ "$(cat "$d/index" 2>/dev/null)" = 0 ] || continue
    [ "$(cat "$d/device/../idVendor" 2>/dev/null)" = 1edb ] || continue
    echo "/dev/${d##*/}"
    return
  done
}

hdmi_connected() {
  grep -qx connected /sys/class/drm/card*-HDMI-A-1/status 2>/dev/null
}

PLACEHOLDER_PID=""

start_placeholder() {
  if [ -n "$PLACEHOLDER_PID" ] && kill -0 "$PLACEHOLDER_PID" 2>/dev/null; then
    return
  fi
  log "камеры нет, показываю заглушку"
  # 640x360 @1fps: почти ноль CPU, kmssink растягивает до экрана аппаратно (HVS).
  gst-launch-1.0 -q videotestsrc pattern=black is-live=true \
    ! video/x-raw,width=640,height=360,framerate=1/1 \
    ! textoverlay text="NO CAMERA" halignment=center valignment=center font-desc="Sans 24" \
    ! kmssink sync=false &
  PLACEHOLDER_PID=$!
}

stop_placeholder() {
  [ -n "$PLACEHOLDER_PID" ] || return
  kill "$PLACEHOLDER_PID" 2>/dev/null
  wait "$PLACEHOLDER_PID" 2>/dev/null
  PLACEHOLDER_PID=""
}

run_pipeline() {
  local caps="image/jpeg,width=${WIDTH},height=${HEIGHT},framerate=${FPS}/1"
  log "камера $CAMERA_DEV, декодер $DECODER"
  if [ "$DECODER" = hw ]; then
    # Аппаратный декод без GStreamer: камера -> пересборка кадров -> bcm2835-codec
    # (быстрый сброс при ложном LAST) -> вывод через DRM. См. usbc-hwbridge.py и docs/01.
    CAMERA_DEV="$CAMERA_DEV" WIDTH="$WIDTH" HEIGHT="$HEIGHT" /usr/local/sbin/usbc-hwbridge.py
    # код 2 — нет дисплея/доступа к DRM: не перезапускать в цикле без паузы
    [ $? -eq 2 ] && sleep 3
  else
    # Программный декод: стабильно, ~5 fps при LOWRES=1 (960x540), ~7 fps при LOWRES=2.
    gst-launch-1.0 -q v4l2src device="$CAMERA_DEV" io-mode="$SRC_IO_MODE" \
      ! "$caps" ! queue max-size-buffers=1 leaky=downstream \
      ! avdec_mjpeg lowres="$LOWRES" \
      ! videoconvert ! video/x-raw,format=I420 \
      ! kmssink sync=false
  fi
  log "пайплайн завершился"
}

trap 'stop_placeholder; exit 0' TERM INT

log "старт"
while true; do
  if ! hdmi_connected; then
    stop_placeholder
    sleep 2
    continue
  fi
  CAMERA_DEV="$(find_camera)"
  if [ -n "$CAMERA_DEV" ]; then
    stop_placeholder
    run_pipeline
    # пауза от дребезга при переподключении USB
    sleep 1
  elif [ "$SECONDS" -lt "$CAMERA_GRACE" ]; then
    # сразу после загрузки камера обычно вот-вот появится: не запускаем GStreamer
    # ради заглушки, пока udev ещё обрабатывает устройства (одно ядро)
    sleep 1
  else
    start_placeholder
    sleep 1
  fi
done
