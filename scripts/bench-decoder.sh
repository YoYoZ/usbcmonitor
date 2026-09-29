#!/bin/bash
# Проверка аппаратного MJPEG-декода (/dev/video10).
#  1. Если камера подключена — средний размер её JPEG-кадра (от него зависит потолок fps).
#  2. Тестовые JPEG 1080p (готовятся один раз) -> v4l2jpegdec -> fakevideosink:
#     потолок fps декодера без копирования (как с kmssink).
#  3. Если HDMI подключён — то же самое через настоящий kmssink.
# Перед запуском: sudo systemctl stop usbc-hdmi-bridge   (декодер и экран должны быть свободны)
set -u

DIR="${BENCH_DIR:-/tmp/jpegbench}"
SECS="${BENCH_SECS:-10}"
CAMERA_DEV="${CAMERA_DEV:-/dev/v4l/by-id/blackmagic-uvc-video-index0}"
CAPS="image/jpeg,width=1920,height=1080,framerate=25/1"
mkdir -p "$DIR"

cpu() { top -bn2 -d 2 | grep -E "^%?Cpu" | tail -n 1 | sed -E 's/^%?Cpu\(s\): *//'; }

if [ -e "$CAMERA_DEV" ]; then
  echo "== кадры камеры ($CAMERA_DEV), 50 кадров"
  v4l2-ctl -d "$CAMERA_DEV" --set-fmt-video=width=1920,height=1080,pixelformat=MJPG \
    --stream-mmap --stream-count=50 --stream-to="$DIR/cam.mjpg" 2>&1 | tail -n 1
  bytes=$(stat -c %s "$DIR/cam.mjpg")
  echo "   средний кадр: $((bytes / 50 / 1024)) КБ  (поток ~$((bytes / 50 * 25 / 1024 / 1024)) МБ/с при 25 fps)"
fi

make_set() {  # имя pattern format quality
  [ -f "$DIR/$1_09.jpg" ] && return
  echo "готовлю $1 ..."
  gst-launch-1.0 -q videotestsrc num-buffers=10 pattern="$2" \
    ! "video/x-raw,width=1920,height=1080,format=$3" \
    ! jpegenc quality="$4" ! multifilesink location="$DIR/$1_%02d.jpg"
}
make_set smpte420  smpte      I420 90
make_set detail422 zone-plate Y42B 85
make_set smpte422  smpte      Y42B 90
make_set snow422   snow       Y42B 75

run() {  # набор sink-описание
  timeout "$SECS" gst-launch-1.0 -v \
    multifilesrc location="$DIR/$1_%02d.jpg" loop=true caps="$CAPS" \
    ! v4l2jpegdec ! queue max-size-buffers=1 leaky=downstream \
    ! fpsdisplaysink video-sink="$2" text-overlay=false sync=false 2>&1 \
    | grep -o "current: [0-9.]*, average: [0-9.]*" | tail -n 1 &
  local pid=$!
  sleep $((SECS / 2))
  local c; c=$(cpu)
  wait "$pid"
  echo "      CPU: $c"
}

SINKS="fakevideosink"
grep -qx connected /sys/class/drm/card*-HDMI-A-1/status 2>/dev/null && SINKS="$SINKS kmssink"

for set in smpte420 smpte422 detail422 snow422; do
  echo "== $set (кадр ~$(du -k "$DIR/${set}_00.jpg" | cut -f1) КБ)"
  for sink in $SINKS; do
    printf "   %-14s " "$sink"; run "$set" "$sink"
  done
done

vcgencmd measure_temp
vcgencmd get_throttled
