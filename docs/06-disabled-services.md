# Список отключённого

Применяется скриптом [scripts/provision-disable.sh](../scripts/provision-disable.sh).
Каждая строка ниже — что отключено и почему, чтобы это не превращалось в
"выключили всё подряд без объяснений".

| Что | Как | Почему |
|---|---|---|
| Bluetooth | `dtoverlay=disable-bt` в `config.txt` + `systemctl disable hciuart bluetooth` | Не используется; на Zero W BT и Wi-Fi используют общий UART/SDIO путь — отключение BT также снимает загрузку `hciuart`, лишний процесс на единственном ядре. |
| Avahi (mDNS) | `systemctl disable --now avahi-daemon avahi-daemon.socket` | Сетевой сервис не нужен в рабочем режиме (сети вообще нет); полезен только в режиме настройки для `usbcmonitor.local` — если мешает, можно временно включать через `usbc-mode-setup.sh`, но по умолчанию оставлен выключенным постоянно, доступ по IP. |
| triggerhappy | `systemctl disable --now triggerhappy.socket triggerhappy.service` | Демон обработки кнопок/GPIO-событий, не используется — на Zero это лишний процесс, слушающий input-устройства. |
| swap (dphys-swapfile) | `systemctl disable --now dphys-swapfile`, `apt purge dphys-swapfile` | На SD-карте своп вреден (износ карты) и не нужен — 512 МБ впритык, но пайплайну свопинг не поможет, только добавит задержку. |
| apt-таймеры | `systemctl disable --now apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service` | Фоновые обновления по расписанию: в рабочем режиме сети нет, они бесполезны, а на этапе настройки создают лишнюю нагрузку/трафик в неподходящий момент. |
| Wi-Fi (в рабочем режиме) | `rfkill block wifi` через `usbc-mode-production.sh` | Требование задачи: без сети в рабочем режиме. Обратимо, см. [05-mode-switching.md](05-mode-switching.md). |
| SSH (в рабочем режиме) | `systemctl stop ssh` через `usbc-mode-production.sh` (автозапуск не трогаем) | Тот же смысл — не слушать порт, когда сети всё равно нет. |
| raspi-config плагины/задачи первого запуска | `systemctl disable userconfig.service` (если присутствует, зависит от версии образа) | Одноразовые сервисы первичной настройки не нужны после первой загрузки. |
| getty на serial0, если используется как console | **Не отключается** | Оставлен намеренно как аварийный канал доступа, см. [05-mode-switching.md](05-mode-switching.md). |
| ModemManager | `systemctl disable --now ModemManager` | Модема нет, а демон опрашивает USB-serial интерфейсы; у составного USB-устройства камеры такие могут быть. Найден включённым на Bookworm Lite. |
| udisks2 | `systemctl disable --now udisks2` | Автомонтирование накопителей не нужно. |
| rpi-eeprom-update | `systemctl disable rpi-eeprom-update` | У Zero нет EEPROM загрузчика (он есть только у Pi 4/5). |
| man-db.timer | `systemctl disable --now man-db.timer` | Переиндексация man-страниц — минуты CPU на одном ядре. |
| getty@tty1 (локальный логин на HDMI) | `systemctl disable --now getty@tty1` | Клавиатуру подключить некуда (единственный USB занят камерой), а приглашение логина мелькало бы на экране между заглушкой и видео. Плюс `vt.global_cursor_default=0` в `cmdline.txt` прячет курсор. Доступ остаётся через serial и SSH. |

## Ускорение загрузки (2026-09-29): картинка через ~24 с вместо ~60 с

| Что | Где | Зачем |
|---|---|---|
| Консоль только в serial, без `console=tty1` | `cmdline.txt` | Нет текста загрузки на HDMI — экран чёрный до видео |
| Убран `fsck.repair=yes`, initramfs выключен (`auto_initramfs=0`), в `fstab` поле проверки = 0 | `cmdline.txt`, `config.txt`, `/etc/fstab` | Проверка ФС после каждого выдёргивания питания занимала ~15 с (initramfs). ext4-журнал целостность метаданных сохраняет; полное решение — корень только для чтения (TODO). ext4/mmc встроены в ядро — без initramfs грузится. |
| `dwc2`, `vc4`, `uvcvideo` грузятся первыми | `/etc/modules-load.d/usbcmonitor.conf` | Без initramfs USB поднимался на 26 с, экран на 53 с |
| Blacklist `cdc_ncm cdc_ether cdc_mbim cdc_wdm snd_usb_audio` | `/etc/modprobe.d/usbcmonitor-blacklist.conf` | Тетеринг и звук камеры не нужны; udev не тратит время на их модули. **Wi-Fi (brcmfmac) не трогается.** |
| `keyboard-setup`, `NetworkManager-wait-online` выключены | systemd | Клавиатуры нет; ждать сеть мосту незачем (Wi-Fi при этом работает) |
| udev-правила `60-triggerhappy`, `69-libmtp` замаскированы (`/etc/udev/rules.d/* -> /dev/null`) | udev | th-cmd падал на каждом устройстве ввода, mtp-probe проверял камеру — лишняя работа |
| `hdmi_group=1`, `hdmi_mode=16` | `config.txt` | Загрузчик выводит тот же 1080p60, что потом KMS — монитор не должен пересинхронизироваться |
| Сервис ищет камеру в sysfs (vendor `1edb`) + `AmbientCapabilities=CAP_DAC_OVERRIDE` | скрипт и юнит | udev при загрузке выставлял права/ссылку на камеру на ~25 с позже ядра; теперь сервис не ждёт udev |
| Заглушка «NO CAMERA» не показывается первые 20 с | скрипт (`CAMERA_GRACE`) | Не грузить CPU стартом GStreamer в самый нагруженный момент загрузки |

Итог по замеру: ядро 3 с, экран (vc4) 19 с, камера у сервиса 21 с, первая картинка ~24 с.

## Пакеты, которые НЕ ставятся

По ограничению задачи "не устанавливать ничего сверх необходимого":
- никакого desktop/X11/Wayland;
- никакого `gstreamer1.0-omx`/`libraspberrypi-bin` заранее — ставится
  только если реально понадобился план Г из
  [04-fallback-paths.md](04-fallback-paths.md);
- никакого `mpv`/`ffmpeg` заранее — только если понадобился план Б/В;
- сетевой стек не меняется: на Bookworm это штатный NetworkManager, на
  Bullseye — `dhcpcd`+`wpa_supplicant`. Скрипты режимов поддерживают оба.
