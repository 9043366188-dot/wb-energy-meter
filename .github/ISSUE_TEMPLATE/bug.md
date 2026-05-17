---
name: Bug report
about: Что-то работает не так
title: ''
labels: bug
assignees: ''
---

## Описание

Кратко, в чём проблема.

## Версия и окружение

- `wb-energy-meter --version`:
- `cat /etc/wb-release`:
- `uname -a`:
- `python3 --version`:
- Установлено через: [tar/install.sh | Install-WbEnergyMeter.cmd | pip | другое]

## Воспроизведение

Шаги, которые приводят к проблеме:

1. ...
2. ...
3. ...

## Что ожидалось

## Что произошло

## Логи

```
journalctl -u wb-energy-meter -n 100 --no-pager
```

```
# Сюда вывод
```

Если падает CLI — вывод `wb-energy-meter-cli ... 2>&1`.

## Дополнительный контекст

Сколько счётчиков, какая нагрузка, что-нибудь необычное в конфиге.
