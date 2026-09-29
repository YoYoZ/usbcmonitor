# Подготовка SD-карты с нуля

> **Фактическое состояние на 2026-09-24.** Imager записал Bookworm (Legacy),
> см. [01-hardware-and-risks.md](01-hardware-and-risks.md). `config.txt`
> и `cmdline.txt` **уже отредактированы на ПК** до первой загрузки
> (оригиналы: `backup/bootfs-original/` и `*.orig` на самой карте).
> В `cmdline.txt` сохранены параметры первой загрузки Imager (`init=...firstboot`,
> `systemd.run=/boot/firstrun.sh` …). Они применяют пользователя, Wi-Fi и SSH
> и удаляются сами после первого старта. **Не заменять `cmdline.txt` целиком
> до первой загрузки.** Шаг 4 ниже для конфигов загрузки поэтому пропускается,
> остаются только скрипты, юниты и udev. На плате пути: `/boot/firmware/...`.

Целевой образ: **Raspberry Pi OS Lite (Legacy, Bullseye), 32-bit armhf**.
Обоснование выбора — [01-hardware-and-risks.md](01-hardware-and-risks.md).

Все шаги — на ПК (Windows/macOS/Linux), кроме отмеченных как "на плате".

## 1. Прошивка образа

1. Установить **Raspberry Pi Imager**.
2. Выбрать: `CHOOSE OS` → `Raspberry Pi OS (other)` → `Raspberry Pi OS Lite (Legacy)` (Bullseye, 32-bit).
3. `CHOOSE STORAGE` → карта для Zero W.
4. **До записи** открыть расширенные настройки (шестерёнка / Ctrl+Shift+X) и задать:
   - hostname: `usbcmonitor` (или своё);
   - включить SSH → "Use password authentication" (или задать свой ключ);
   - имя пользователя/пароль (не оставлять `pi`/`raspberry`);
   - настроить Wi-Fi (SSID/пароль/страна) — понадобится только на этапе
     настройки, потом будет выключаться, см. [05-mode-switching.md](05-mode-switching.md);
   - локаль/часовой пояс по необходимости.
5. Записать образ.

Это заменяет ручное создание `ssh`/`userconf.txt`/`wpa_supplicant.conf`
в `/boot` — Imager делает это автоматически и надёжнее.

## 2. Первая загрузка и подключение

1. Вставить карту в Zero W, подключить питание (только power-порт).
2. Подождать ~1–2 минуты, найти плату в сети (роутер / `ping usbcmonitor.local`,
   на Bullseye Avahi ещё стоит по умолчанию и ещё не отключён — это нормально
   на данном этапе).
3. Подключиться:

```bash
ssh <user>@usbcmonitor.local
```

4. Обновить пакеты (**один раз**, при наличии сети, не в рабочем режиме):

```bash
sudo apt update && sudo apt full-upgrade -y
sudo rpi-update -y  # только если требуется свежая прошивка VideoCore для JPEG-декодера; см. ниже
sudo reboot
```

> `rpi-update` трогает прошивку/ядро мимо `apt` — использовать один раз,
> только если на шаге 4 (`v4l2-ctl --list-formats-out`) декодер не покажет
> MJPG в списке форматов на стоковом ядре Bullseye. Если MJPG есть сразу —
> `rpi-update` не нужен, лишний компонент по требованию задачи "не ставить
> ничего сверх необходимого".

## 3. Установка пакетов пайплайна

```bash
sudo apt install -y --no-install-recommends \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-x \
  gstreamer1.0-libav \
  v4l-utils
```

`gstreamer1.0-libav` — программный MJPEG-декодер `avdec_mjpeg` (режим по умолчанию,
см. README). Тянет ~25 библиотек FFmpeg (`libavformat`, `libavfilter` и др.).

`gstreamer1.0-x` нужен только ради `textoverlay` (надпись «NO CAMERA»): в Debian
плагин Pango лежит в этом пакете (+`libxv1`, ~350 КБ, X-сервер не ставится).
`gstreamer1.0-plugins-bad` в Debian тянет Mesa/LLVM как зависимости (~100 МБ
на диске). При работе из этого ничего не запускается.

Пояснение по составу (без ничего лишнего):
- `gstreamer1.0-plugins-good` — содержит `v4l2src` и общий `video4linux2`
  плагин, который на лету регистрирует `v4l2jpegdec`/`v4l2convert`, если в
  системе есть подходящее M2M-устройство.
- `gstreamer1.0-plugins-bad` — здесь живёт `kmssink`.
- `v4l-utils` — `v4l2-ctl` для диагностики и настройки.
- Никакого X11, Wayland, PulseAudio, desktop-метапакетов не ставится.

Если план Б (OMX) понадобится — пакеты для него описаны отдельно в
[04-fallback-paths.md](04-fallback-paths.md), не ставить их заранее "про запас".

## 4. Перенос конфигурации из этого репозитория на плату

С ПК (замените `<user>@usbcmonitor.local` на свои данные):

```bash
scp config/config.txt      <user>@usbcmonitor.local:/tmp/config.txt
scp config/cmdline.txt     <user>@usbcmonitor.local:/tmp/cmdline.txt
scp udev/99-blackmagic-uvc.rules <user>@usbcmonitor.local:/tmp/
scp systemd/usbc-hdmi-bridge.service <user>@usbcmonitor.local:/tmp/
scp scripts/usbc-hdmi-bridge.sh scripts/diagnose.sh scripts/mode-setup.sh scripts/mode-production.sh scripts/provision-disable.sh \
    <user>@usbcmonitor.local:/tmp/
```

На плате:

```bash
# config.txt и cmdline.txt — сначала посмотреть diff с текущими, не затирать вслепую
diff /boot/config.txt /tmp/config.txt
diff /boot/cmdline.txt /tmp/cmdline.txt
```

**Критично:** `config/cmdline.txt` в этом репозитории содержит плейсхолдер
`root=PARTUUID=REPLACE-WITH-YOUR-PARTUUID` — его нужно заменить реальным
PARTUUID вашей карты (посмотреть в текущем `/boot/cmdline.txt` на плате
командой `cat /boot/cmdline.txt`, скопировать оттуда `root=PARTUUID=...`
как есть). Прошивка cmdline.txt с чужим/вымышленным PARTUUID даёт
неспособную загрузиться систему.

```bash
sudo cp /boot/config.txt  /boot/config.txt.orig
sudo cp /boot/cmdline.txt /boot/cmdline.txt.orig
sudo cp /tmp/config.txt  /boot/config.txt
sudo cp /tmp/cmdline.txt /boot/cmdline.txt   # только после правки PARTUUID выше!

sudo install -m 755 /tmp/usbc-hdmi-bridge.sh   /usr/local/sbin/usbc-hdmi-bridge.sh
sudo install -m 755 /tmp/diagnose.sh           /usr/local/sbin/usbc-diagnose.sh
sudo install -m 755 /tmp/mode-setup.sh         /usr/local/sbin/usbc-mode-setup.sh
sudo install -m 755 /tmp/mode-production.sh    /usr/local/sbin/usbc-mode-production.sh
sudo install -m 755 /tmp/provision-disable.sh  /usr/local/sbin/usbc-provision-disable.sh

sudo install -m 644 /tmp/usbc-hdmi-bridge.service /etc/systemd/system/usbc-hdmi-bridge.service
sudo install -m 644 /tmp/99-blackmagic-uvc.rules  /etc/udev/rules.d/99-blackmagic-uvc.rules

sudo udevadm control --reload-rules
sudo systemctl daemon-reload
```

## 5. Отключение лишнего (документированный список)

Выполнить скрипт провижининга (идемпотентный, можно гонять повторно):

```bash
sudo /usr/local/sbin/usbc-provision-disable.sh
```

Полный список того, что отключается, и почему — в
[06-disabled-services.md](06-disabled-services.md). Ничего не отключается
"втихую" мимо этого списка.

## 6. Включение сервиса и первая перезагрузка в рабочем режиме

```bash
sudo systemctl enable usbc-hdmi-bridge.service
sudo /usr/local/sbin/usbc-mode-production.sh   # выключает Wi-Fi/SSH-доступность
sudo reboot
```

После этого перезагрузки плата стартует без сети, без десктопа, сразу
поднимая `usbc-hdmi-bridge.service`. Как вернуться в режим настройки —
[05-mode-switching.md](05-mode-switching.md).

## 6а. Ускорение загрузки и рабочий режим (только для чтения)

Файлы — в `config/` репозитория, пояснения — в [06-disabled-services.md](06-disabled-services.md)
и [05-mode-switching.md](05-mode-switching.md). Порядок важен: всё, что пишет в `/etc` и
`/boot`, — **до** включения overlay.

```bash
sudo cp config/modules-load-usbcmonitor.conf       /etc/modules-load.d/usbcmonitor.conf
sudo cp config/modprobe-usbcmonitor-blacklist.conf /etc/modprobe.d/usbcmonitor-blacklist.conf
sudo mkdir -p /etc/systemd/journald.conf.d && sudo cp config/journald-usbcmonitor.conf /etc/systemd/journald.conf.d/usbcmonitor.conf
sudo cp config/initramfs-usbcmonitor.conf          /etc/initramfs-tools/conf.d/usbcmonitor   # MODULES=most
sudo ln -sf /dev/null /etc/udev/rules.d/60-triggerhappy.rules
sudo ln -sf /dev/null /etc/udev/rules.d/69-libmtp.rules
sudo systemctl disable keyboard-setup NetworkManager-wait-online
# /etc/fstab: поле проверки = 0 для обоих разделов, для /boot/firmware — defaults,ro (см. config/fstab)
# cmdline.txt: убрать console=tty1 и fsck.repair=yes (см. config/cmdline.txt)
sudo apt install -y --no-install-recommends overlayroot   # пересоберёт initramfs для всех ядер, ~10 мин
sync
sudo usbc-mode-production.sh && sudo reboot
```

Без `MODULES=most` initramfs на живой системе не собирается (`mkinitramfs: failed to
determine device for /`: корень виден как `/dev/root`).

## 7. Первая проверка (обязательно, до всякой автоматизации)

Прежде чем полагаться на systemd-автозапуск, пройти вручную процедуру
[03-debug-procedure.md](03-debug-procedure.md) шаги 1–7 **в режиме настройки**
(Wi-Fi включён, сервис можно временно остановить: `sudo systemctl stop
usbc-hdmi-bridge.service`), чтобы не гадать, а убедиться, что каждый шаг
реально отдаёт то, что ожидается.
