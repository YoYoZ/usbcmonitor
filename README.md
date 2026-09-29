# USB(UVC) Blackmagic → HDMI мост на Raspberry Pi Zero W

Pi Zero W принимает 1080p25 MJPEG по UVC с Blackmagic Pocket Cinema Camera 6K и
выводит на HDMI-монитор через аппаратный декодер VideoCore IV, без десктопа,
с автозапуском. Wi-Fi и SSH включены всегда (решение владельца).

## Состояние на 2026-09-29 — готово, проверено на мониторе

- **~22–25 fps на экране в 1920x1080** (зависит от сложности сцены: на тяжёлых кадрах
  упирается в потолок аппаратного декодера), CPU ~50%. Глитчей и замираний нет,
  изредка пропущенный кадр при 25 fps незаметен.
- Холодный старт до картинки ~31 с, без текста на экране, монитор переключается один раз.
- Горячее переподключение камеры — работает («NO CAMERA» → видео).
- **Рабочий режим: корень только для чтения** (overlayroot): выдёргивание питания
  ничего не портит. Менять что-то на плате — через режим настройки,
  см. [docs/05-mode-switching.md](docs/05-mode-switching.md).

Как устроено: программа [scripts/hwbridge.py](scripts/hwbridge.py) (только
стандартная библиотека Python, без GStreamer) — камера (V4L2) → пересборка кадров →
`bcm2835-codec` → overlay-плоскость DRM/KMS без копирования.

Особенности камеры (BMPCC 6K Gen 1 на бете 8.6 — единственной прошивке с UVC для
этой модели, исправлений не будет), которые программа обходит:
1. кадры приходят со сдвигом на 64 байта → пересборка кадров по маркерам SOI..EOI;
2. в ~10 кадрах в минуту вырван кусок данных (буфер не кратен 64 байтам) → такие
   кадры не отдаются декодеру, на экране на 40 мс остаётся предыдущий;
3. если битый кадр всё же прошёл и декодер выдал ложный «конец потока» → сброс
   декодера за доли секунды (с фильтром п.2 — 0 сбросов за проверку).

Программный декод (`DECODER=sw`, ~4,5 fps, 960x540) оставлен запасным:
```bash
sudo systemctl set-environment DECODER=sw && sudo systemctl restart usbc-hdmi-bridge
```
Подробности расследования — [docs/01-hardware-and-risks.md](docs/01-hardware-and-risks.md),
ускорение загрузки — [docs/06-disabled-services.md](docs/06-disabled-services.md).

Кабель — только через USB-A: `камера USB-C → кабель C–A → OTG
micro-USB–A(мама) → порт USB Zero`. Переходник micro-USB→USB-C не работает.
Камеру питать от сети или полной батареи (на разряде рвёт USB).

## Управление: зеркало и сетки

Одна кнопка **между пином 39 (GND) и пином 40 (GPIO21)** — последняя пара гребёнки,
без резисторов (подтяжка внутренняя, включается программно). Проверено простым
замыканием контактов.

| Действие | Что делает |
|---|---|
| Короткое нажатие | Зеркало вкл/выкл (как в селфи-камере — удобно выставлять кадр на себя) |
| Долгое нажатие (> 0,8 с) | Следующая разметка: выкл → трети + центр → 2.39:1 → 4:5 → выкл |

Всё аппаратно (HVS: отражение плоскости, отдельная прозрачная плоскость под разметку),
видео и нагрузка на CPU не страдают. При включении — без зеркала и без разметки.

Без кнопки, по SSH (шаблон с `^python3`, чтобы не задеть собственную команду):
```bash
sudo pkill -USR1 -f "^python3 /usr/local/sbin/usbc-hwbridge.py"   # зеркало
sudo pkill -USR2 -f "^python3 /usr/local/sbin/usbc-hwbridge.py"   # следующая разметка
```
Начальное состояние и пин меняются переменными в юните: `MIRROR=1`, `GRID=thirds|2.39:1|4:5`,
`BUTTON_GPIO=` (пусто — без кнопки).

Доступ: `ssh -i ~/.ssh/id_ed25519_usbcmonitor admin@<ip>`, hostname `TypeCMonitor`.

## Тест с камерой и монитором

1. Подключить монитор кабелем HDMI-HDMI, **включить его**, потом подать
   питание на Pi (так ядро прочитает EDID при загрузке; подключение монитора
   позже тоже подхватывается). Через ~35 с — «NO CAMERA» на чёрном фоне.
2. Подключить камеру через OTG-переходник в порт USB (не PWR). Картинка должна
   появиться через несколько секунд.
3. С ПК: `ssh admin@<ip>`, дальше по ситуации:

| Что | Команда на плате |
|---|---|
| Что делает сервис | `journalctl -u usbc-hdmi-bridge -f` |
| Полный диагностический лог (прислать мне) | `sudo usbc-diagnose.sh` → `/var/log/usbc-diagnose-*.log` |
| Размер кадров камеры и потолок декодера | `sudo systemctl stop usbc-hdmi-bridge && sudo usbc-bench-decoder.sh; sudo systemctl start usbc-hdmi-bridge` |
| Текущий HDMI-режим | `sudo usbc-hdmi-mode.sh` |
| 1080p50 (лучше для 25 fps) / принудительно | `sudo usbc-hdmi-mode.sh 1080p50 && sudo reboot` (или `force1080p60`, `force720p60`, `auto`) |
| Какой режим выбран сейчас | `cat /sys/class/graphics/fb0/virtual_size` |
| Что отдаёт монитор (EDID) | `cat /sys/class/drm/card0-HDMI-A-1/modes` |

4. Замерить задержку (ниже).
5. Плата в рабочем режиме (корень только для чтения): любые правки пропадут после
   перезагрузки. Чтобы что-то изменить — `sudo usbc-mode-setup.sh && sudo reboot`,
   правки, `sync`, потом `sudo usbc-mode-production.sh && sudo reboot`.
   См. [docs/05-mode-switching.md](docs/05-mode-switching.md). Wi-Fi и SSH работают всегда.

## Структура репозитория

```
docs/
  01-hardware-and-risks.md   — допущения, результаты проверки на плате, риски
  02-sd-card-setup.md        — подготовка SD-карты с нуля
  03-debug-procedure.md      — 8 шагов проверки
  04-fallback-paths.md       — запасные пути (v4l2convert, mpv, ffmpeg, OMX)
  05-mode-switching.md       — режим настройки <-> рабочий режим
  06-disabled-services.md    — что отключено и почему
config/                      — копии с платы
  config.txt                 — dwc2 host, gpu_mem=128, vc4-kms-v3d cma-192, disable-bt, hdmi_mode=16, initramfs вкл.
  cmdline.txt                — overlayroot=tmpfs, fastboot, video= auto, quiet, консоль только serial
  fstab                      — без проверки ФС, /boot/firmware только для чтения (-> /etc/fstab)
  modules-load-usbcmonitor.conf       — dwc2 vc4 uvcvideo первыми (-> /etc/modules-load.d/usbcmonitor.conf)
  modprobe-usbcmonitor-blacklist.conf — тетеринг и звук камеры (-> /etc/modprobe.d/usbcmonitor-blacklist.conf)
  journald-usbcmonitor.conf  — журнал в RAM, до 16 МБ (-> /etc/systemd/journald.conf.d/usbcmonitor.conf)
  initramfs-usbcmonitor.conf — MODULES=most, иначе initramfs не собирается (-> /etc/initramfs-tools/conf.d/usbcmonitor)
  networkmanager-wifi-powersave-off.conf — без энергосбережения Wi-Fi, иначе SSH отваливается (-> /etc/NetworkManager/conf.d/)
backup/bootfs-original/      — исходные config.txt/cmdline.txt от Imager
systemd/
  usbc-hdmi-bridge.service   — автозапуск, Restart=always, пользователь bridge, песочница
udev/
  99-blackmagic-uvc.rules    — symlink камеры по vendor ID 1edb, её сетевой интерфейс мимо NetworkManager
scripts/                     — на плате лежат как /usr/local/sbin/usbc-*
  usbc-hdmi-bridge.sh        — основной цикл: заглушка <-> декод, hotplug камеры и монитора
  diagnose.sh                — полный диагностический лог одним запуском
  bench-decoder.sh           — размер кадров камеры, потолок fps декодера
  hwbridge.py                — основной режим (DECODER=hw): камера -> пересборка -> bcm2835-codec -> DRM
  hwdec_probe.py             — диагностика: камера -> bcm2835-codec без вывода, счёт LAST, DUMP кадров
  hdmi-mode.sh               — переключение HDMI-режима (video= в cmdline.txt)
  mode-setup.sh              — выключить корень только для чтения (для правок), нужна перезагрузка
  mode-production.sh         — включить корень только для чтения (overlayroot), нужна перезагрузка
  provision-disable.sh       — отключение лишних сервисов, создание пользователя bridge
```

## Основной пайплайн

По умолчанию — программный декод (`DECODER=sw`), стабильно, ~4,5 fps:

```bash
gst-launch-1.0 v4l2src device=/dev/v4l/by-id/blackmagic-uvc-video-index0 io-mode=mmap \
  ! image/jpeg,width=1920,height=1080,framerate=25/1 \
  ! queue max-size-buffers=1 leaky=downstream \
  ! avdec_mjpeg lowres=1 \
  ! videoconvert ! video/x-raw,format=I420 \
  ! kmssink sync=false
```

- `lowres=1` — JPEG декодируется сразу в 960x540: вдвое меньше работы IDCT,
  а монитор всё равно 1024x600. `LOWRES=2` (480x270) — ~7 fps, но мыльно.
  Узкое место — энтропийный декод 1080p на одном ядре ARMv6, его не ускорить.
- `queue ... leaky` — декодер не успевает за 25 fps камеры, лишние кадры
  выбрасываются до декодера, задержка не копится.
- `videoconvert` в I420: vc4 не выделяет буфер экрана под планарный 4:2:2 (Y42B),
  а аппаратный ISP 4:2:2-planar на вход не принимает.

Аппаратный вариант (`DECODER=hw`, 25 fps, нестабилен) — см. скрипт и docs/01.

Параметры переопределяются переменными окружения в systemd-юните
(`DECODER`, `LOWRES`, `SRC_IO_MODE`, `CAMERA_DEV`, `WIDTH`/`HEIGHT`/`FPS`).

## Как проверить fps

- Потолок декодера и размер кадров камеры: `usbc-bench-decoder.sh` (таблица выше).
- Живой поток: `sudo usbc-diagnose.sh` — 20 секунд пайплайна с
  `GST_DEBUG=3,v4l2*:5` и `fpsdisplaysink`, результат в логе. Сервис моста
  скрипт на время теста останавливает и потом запускает сам.

## Как измерить задержку

1. На телефоне включить секундомер с миллисекундами.
2. Направить на него камеру Blackmagic.
3. Сфотографировать **одним кадром** и телефон, и монитор моста.
4. Разница показаний (секундомер «вживую» минус секундомер на мониторе) —
   задержка тракта камера → USB → декод → HDMI (плюс внутренняя задержка самой
   камеры и монитора).
5. Повторить 5–10 раз, взять медиану: отдельный снимок может попасть на границу
   кадра и ошибиться на 1/25 с.

Ориентир: передача кадра по USB2 (десятки мс) + аппаратный декод (~30 мс для
4:2:2 1080p) + вывод (до одного кадра). Сотни миллисекунд — признак того, что
где-то копится буферизация.

## Известные ограничения

- Программный декод 1080p25 MJPEG на ARMv6 невозможен, всё держится на
  аппаратном `bcm2835-codec`. Если кадры камеры очень тяжёлые (~1 МБ), потолок
  декодера ниже 25 fps — тогда снижать качество MJPEG на стороне камеры.
- `gstreamer1.0-plugins-bad` в Debian тянет Mesa/LLVM (~100 МБ на диске), в
  работе они не участвуют.
- Нулевой задержки нет: всё сведено к отключению синхронизации и очередям в
  один буфер.
- В рабочем режиме доступ только через serial-консоль или карту.
