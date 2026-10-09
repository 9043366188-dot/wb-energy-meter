# Браузерные тесты в Linux-песочнице

Читайте этот файл, когда запускаете tests/browser/run.py в Linux-среде без
системных библиотек Chromium или если Playwright сообщает TargetClosedError
при запуске браузера с отсутствующим libXdamage.so.1.

Playwright нужен только для разработки и тестов; в runtime и pyproject.toml
он не входит. Если у среды нет sudo, браузер можно установить без
playwright install --with-deps, распаковав нужные системные пакеты рядом:

~~~bash
pip install playwright
python3 -m playwright install chromium
mkdir -p /tmp/xlibs && cd /tmp/xlibs
apt-get download libxdamage1 libxfixes3 libxrandr2 libxcomposite1 \
  libxkbcommon0 libatk1.0-0 libatk-bridge2.0-0 libcups2 libatspi2.0-0 \
  libasound2 libpango-1.0-0 libcairo2 libnspr4 libnss3 libdrm2 libgbm1
for d in *.deb; do dpkg-deb -x "$d" /tmp/xroot; done
export LD_LIBRARY_PATH=/tmp/xroot/usr/lib/x86_64-linux-gnu:/tmp/xroot/lib/x86_64-linux-gnu
cd /path/to/wb-energy-meter
python3 tests/browser/run.py
~~~

Без LD_LIBRARY_PATH Chromium может завершиться с ошибкой загрузки
libXdamage.so.1, которую Playwright показывает как
TargetClosedError: BrowserType.launch. В этом случае проблема связана с
системной библиотекой, а не с самим браузерным сценарием.