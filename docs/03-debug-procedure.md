# Порядок отладки — проверять, не гадать

Каждый шаг — конкретная команда и то, что считается успехом. Выполнять по
порядку на плате в **режиме настройки** (см. [05-mode-switching.md](05-mode-switching.md)),
сервис `usbc-hdmi-bridge` можно временно остановить:

```bash
sudo systemctl stop usbc-hdmi-bridge.service
```

Заполняйте колонку "Результат на вашем железе" по факту — этот файл рассчитан
на то, чтобы вписывать реальные значения (vendor/product ID, имена
`/dev/videoN`), а не оставлять предположения авторов инструкции.

## Шаг 1 — камера видна как High Speed USB, драйвер uvcvideo подхватился

```bash
lsusb -t
dmesg | grep -i -E "uvcvideo|usb.*high.speed" | tail -n 40
```

Успех: в дереве `lsusb -t` камера висит на шине с `5000M`/`480M` — для UVC
1080p нужен именно **480M (High Speed)**, а не 12M (Full Speed) — на Full
Speed 1080p25 MJPEG физически не пролезет по пропускной способности.
В `dmesg` должна быть строка вида `uvcvideo: Found UVC ... device`.

Если камера не видна вообще — проверить: OTG-переходник поддерживает host-режим
(не просто "charge only" кабель), `dwc2` в `dr_mode=host` (см.
[config/config.txt](../config/config.txt)), питание камеры отдельное и
достаточное (Blackmagic 6K может требовать больше тока, чем способен дать
Zero через сигнальные линии OTG — это ожидаемо, т.к. Zero не запитывает
камеру, у камеры своё питание).

**Результат на вашем железе:** _(заполнить)_

## Шаг 2 — найти устройство камеры и bcm2835-codec-decode

```bash
v4l2-ctl --list-devices
ls -l /dev/v4l/by-id/
ls -l /dev/v4l/by-path/
```

Успех: в списке есть запись камеры (обычно
`Blackmagic ... (usbX):` или просто по названию UVC-контроллера камеры) с
`/dev/videoN`, и отдельная запись `bcm2835-codec-decode` с `/dev/video1N`
(как правило `/dev/video10`).

Использовать **стабильный** путь `/dev/v4l/by-id/usb-<...>-video-index0`,
а не `/dev/videoN` — номер может меняться между загрузками/переподключениями.
Записать точное имя symlink'а — оно нужно в
[scripts/usbc-hdmi-bridge.sh](../scripts/usbc-hdmi-bridge.sh) и в udev-правиле.

**Результат на вашем железе:**
- Камера: `/dev/v4l/by-id/` = _(заполнить; udev-правило добавляет `blackmagic-uvc-video-index0`)_
- Декодер: **`/dev/video10` = `bcm2835-codec-decode`** (проверено 2026-09-25, Bookworm, ядро 6.12.109)

## Шаг 3 — камера отдаёт MJPG 1920x1080@25

```bash
v4l2-ctl -d /dev/v4l/by-id/<камера> --list-formats-ext
```

Успех: в выводе есть блок `'MJPG'` (Motion-JPEG) с размером `1920x1080` и
интервалом кадра, соответствующим 25 fps (`Interval: Discrete 0.040s (25.000 fps)`).

**Результат на вашем железе:** _(вставить вывод команды)_

## Шаг 4 — декодер принимает MJPG на входе (OUTPUT)

Это самый важный шаг — именно он подтверждает или опровергает главное
допущение проекта (см. [01-hardware-and-risks.md](01-hardware-and-risks.md)).

```bash
v4l2-ctl -d /dev/video10 --list-formats-out
```

Успех: в списке форматов OUTPUT (то, что декодер принимает на вход) есть
`'MJPG'`.

- **Если MJPG есть** → основной пайплайн из README имеет смысл пробовать дальше (шаг 5+).
- **Если MJPG нет, есть только H264** → аппаратный M2M-путь для MJPEG на
  этой прошивке/ядре не работает, переходить сразу к
  [04-fallback-paths.md](04-fallback-paths.md) (сначала пробовать
  `rpi-update` на предмет более новой прошивки VideoCore с MJPEG-декодером,
  затем OMX/mmal путь).

**Результат на вашем железе (2026-09-25): ПРОЙДЕН.**
OUTPUT: `MPG4`, `H264`, `MJPG`, `H263`. CAPTURE: `YU12`, `YV12`, `NV12`, `NV21`,
`NC12`, `RGBP`, `AB24`, `BGR4`.

Важно: сам факт наличия MJPG ещё не значит, что декодер запустится. Нужен
`gpu_mem=128` в `config.txt`, иначе при старте потока в `dmesg` будет
`failed to create component ril.video_decode (Not enough GPU mem?)`, а в
GStreamer — `Failed to allocate required memory`. См. [01-hardware-and-risks.md](01-hardware-and-risks.md).

## Шаг 5 — снять один кадр в файл

```bash
v4l2-ctl -d /dev/v4l/by-id/<камера> \
  --set-fmt-video=width=1920,height=1080,pixelformat=MJPG \
  --stream-mmap --stream-count=1 --stream-to=/tmp/frame.jpg
```

Скопировать файл на ПК и открыть:

```bash
scp <user>@usbcmonitor.local:/tmp/frame.jpg .
```

Успех: файл открывается, картинка соответствует тому, что видит камера
(не серый прямоугольник, не битые блоки JPEG).

**Результат на вашем железе:** _(вставить: открылся/не открылся, размер файла)_

## Шаг 6 — декод в fakevideosink, держит ли 25 fps и какая нагрузка CPU

```bash
gst-launch-1.0 -v v4l2src device=/dev/v4l/by-id/<камера> io-mode=mmap \
  ! image/jpeg,width=1920,height=1080,framerate=25/1 \
  ! v4l2jpegdec \
  ! fpsdisplaysink video-sink=fakevideosink text-overlay=false sync=false &
sleep 15
top -bn1 | head -n 15
kill %1
```

Успех: `fpsdisplaysink` печатает `current: 25.00, average: ~25.00` (не
падает до 10–15), `top` показывает загрузку CPU (одно ядро!) — на Zero
разумно ожидать 40–80% на самом декоде/копированиях буферов; если
устойчиво under 100% и fps держится — путь рабочий.

Если `v4l2jpegdec` падает с ошибкой `Failed to allocate required memory`,
`VIDIOC_REQBUFS failed` или зависает без кадров — это тот самый известный
баг с конкретными камерами (см. ссылки в
[01-hardware-and-risks.md](01-hardware-and-risks.md)) → пробовать
`io-mode=dmabuf` вместо `mmap`, затем — план Б.

**Результат на вашем железе:** без камеры декодер проверен на тестовых
JPEG (`usbc-bench-decoder.sh`): 28–38 fps на обычных кадрах 1080p, ~20 fps на
экстремально тяжёлых (~1 МБ). С `fakesink` CPU ~80% из-за копии каждого кадра
(у него нет video meta), с `fakevideosink`/`kmssink` копии нет, CPU 16–38%.
Поэтому для замеров используйте `fakevideosink`, а не `fakesink`.
С камерой: _(заполнить)_

## Шаг 7 — вывод на kmssink

```bash
gst-launch-1.0 -v v4l2src device=/dev/v4l/by-id/<камера> io-mode=dmabuf \
  ! image/jpeg,width=1920,height=1080,framerate=25/1 \
  ! v4l2jpegdec \
  ! kmssink sync=false
```

Успех: картинка с камеры появляется на HDMI-мониторе. Если экран чёрный, но
процесс не падает — проверить, что ничего другого не держит DRM-master
(`fuser /dev/dri/card0`, getty на tty1 обычно не конфликтует, но лишний
`fbcon`/plymouth — может).

Если `v4l2jpegdec ! kmssink` не согласовывают формат (`not negotiated`) —
см. план Б №1 в [04-fallback-paths.md](04-fallback-paths.md) (явный формат
+ `v4l2convert`).

**Результат на вашем железе:** _(картинка появилась / чёрный экран / ошибка согласования)_

## Шаг 8 — настройка задержки

После того как шаги 1–7 подтверждены рабочими, применить:

```bash
gst-launch-1.0 -v v4l2src device=/dev/v4l/by-id/<камера> io-mode=dmabuf \
  ! image/jpeg,width=1920,height=1080,framerate=25/1 \
  ! queue max-size-buffers=1 leaky=downstream \
  ! v4l2jpegdec \
  ! queue max-size-buffers=1 leaky=downstream \
  ! kmssink sync=false
```

- `sync=false` — не ждать таймстемпы, выводить кадры как только готовы.
- `queue ... leaky=downstream max-size-buffers=1` — не копить буферы: если
  вывод не успевает, старые кадры выбрасываются, а не накапливают задержку.
- `io-mode=dmabuf` на стороне `v4l2src` — меньше копирований память→память
  между USB-приёмом и декодером.

Это ровно то, что реализовано в
[scripts/usbc-hdmi-bridge.sh](../scripts/usbc-hdmi-bridge.sh) — после
подтверждения на этом шаге можно переходить к автозапуску.

Замер реальной задержки — см. README, раздел "Измерение задержки".
