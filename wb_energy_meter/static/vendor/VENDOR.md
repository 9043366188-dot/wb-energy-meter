# Вендоренные библиотеки (ТЗ v0.11.0, §4)

Все файлы ниже скачаны один раз с официального CDN по точным версиям и
положены в этот каталог, чтобы веб-интерфейс работал **без интернета в
браузере** (контроллер стоит в изолированной сети объекта). При
обновлении версии — повторить закачку по тем же URL с новым номером
версии и пересчитать SHA-256.

| Файл | Версия | Источник | SHA-256 |
|---|---|---|---|
| `leaflet.js` | 1.9.4 | https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js | `db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a` |
| `leaflet.css` | 1.9.4 | https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css | `a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6` |
| `images/marker-icon.png` | 1.9.4 | https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/images/marker-icon.png | `574c3a5cca85f4114085b6841596d62f00d7c892c7b03f28cbfa301deb1dc437` |
| `images/marker-icon-2x.png` | 1.9.4 | https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/images/marker-icon-2x.png | `00179c4c1ee830d3a108412ae0d294f55776cfeb085c60129a39aa6fc4ae2528` |
| `images/marker-shadow.png` | 1.9.4 | https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/images/marker-shadow.png | `264f5c640339f042dd729062cfc04c17f8ea0f29882b538e3848ed8f10edb4da` |
| `images/layers.png` | 1.9.4 | https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/images/layers.png | `1dbbe9d028e292f36fcba8f8b3a28d5e8932754fc2215b9ac69e4cdecf5107c6` |
| `images/layers-2x.png` | 1.9.4 | https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/images/layers-2x.png | `066daca850d8ffbef007af00b06eac0015728dee279c51f3cb6c716df7c42edf` |
| `leaflet-geoman.min.js` | 2.20.0 (`@geoman-io/leaflet-geoman-free`) | https://cdn.jsdelivr.net/npm/@geoman-io/leaflet-geoman-free@2.20.0/dist/leaflet-geoman.min.js | `9d903788bf18e9c78f2577f1cb4594a545d7cca8fcbdc4f009893186a2929b8c` |
| `leaflet-geoman.css` | 2.20.0 | https://cdn.jsdelivr.net/npm/@geoman-io/leaflet-geoman-free@2.20.0/dist/leaflet-geoman.css | `a0df8e0f6301f9f71d772b2c61913bb9094ae0735fd2223ccd1aecd871ea250c` |
| `alpine.min.js` | 3.13.0 (`alpinejs`) | https://cdn.jsdelivr.net/npm/alpinejs@3.13.0/dist/cdn.min.js | `39a70fa6e59b652767821313a37a873c197222ba636397ed064d4c9a3ac539ed` |

## Лицензии

- `LICENSE-leaflet` — Leaflet, BSD-2-Clause.
- `LICENSE-geoman` — Leaflet-Geoman Free, MIT.
- `LICENSE-alpine` — Alpine.js, MIT.

## Проверка целостности

```bash
cd wb_energy_meter/static/vendor
sha256sum leaflet.js leaflet.css leaflet-geoman.min.js leaflet-geoman.css \
          alpine.min.js images/*.png
```

Сверить со значениями в таблице выше.

## Почему так

Раньше `index.html` тянул Alpine.js с `cdn.jsdelivr.net`. На объекте без
интернета в браузере это означало белый экран. Карта плана (Leaflet +
Leaflet-Geoman) добавила бы вторую такую зависимость, поэтому решено
раз и навсегда вендорить все три библиотеки локально — см.
`docs/TZ-v0.11.0-site-plan.md`, §4.
