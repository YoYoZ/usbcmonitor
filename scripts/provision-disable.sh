#!/bin/bash
# Идемпотентный провижининг: отключает лишнее по списку docs/06-disabled-services.md
# и создаёт системного пользователя для usbc-hdmi-bridge.service.
set -uo pipefail   # без -e: на разных образах части юнитов может не быть

echo "== системный пользователь bridge =="
if ! id bridge >/dev/null 2>&1; then
  useradd --system --user-group --no-create-home --home-dir /var/cache/usbc-bridge \
    --shell /usr/sbin/nologin --groups video,render bridge
else
  usermod --groups video,render bridge
fi

echo "== bluetooth =="
systemctl disable --now hciuart.service bluetooth.service 2>/dev/null

echo "== avahi =="
systemctl disable --now avahi-daemon.service avahi-daemon.socket 2>/dev/null

echo "== triggerhappy =="
systemctl disable --now triggerhappy.socket triggerhappy.service 2>/dev/null

echo "== swap =="
systemctl disable --now dphys-swapfile.service 2>/dev/null
swapoff -a 2>/dev/null

echo "== apt-таймеры =="
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null
systemctl disable --now apt-daily.service apt-daily-upgrade.service 2>/dev/null

echo "== одноразовый сервис первого запуска =="
systemctl disable userconfig.service 2>/dev/null

echo "== ModemManager / udisks2 / eeprom / man-db =="
# ModemManager опрашивает USB-serial интерфейсы — у составного USB-устройства камеры
# они могут быть. udisks2 (автомонтирование), rpi-eeprom-update (у Zero нет EEPROM)
# и man-db.timer (переиндексация man, минуты CPU на Zero) не нужны.
systemctl disable --now ModemManager.service udisks2.service rpi-eeprom-update.service man-db.timer 2>/dev/null

echo "== getty на HDMI (tty1) =="
# Клавиатуру подключить некуда (единственный USB занят камерой), а приглашение
# логина мелькает на экране между заглушкой и видео. Serial и SSH остаются.
systemctl disable --now getty@tty1.service 2>/dev/null

echo "== включено сейчас =="
systemctl list-unit-files --state=enabled --no-legend | awk '{print $1}'
