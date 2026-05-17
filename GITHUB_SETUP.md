# Как залить проект на GitHub (с нуля)

Этот файл — одноразовая инструкция для первого пуша. После того как
репозиторий создан, этот файл можно удалить.

## Что нужно

- Аккаунт на github.com (если нет — зарегистрируйтесь).
- Установленный `git` на компьютере (https://git-scm.com/downloads).
- Базовые команды в терминале.

## Шаг 1. Создать пустой репозиторий на GitHub

1. Зайдите на https://github.com и нажмите `+` → `New repository`.
2. **Имя**: `wb-energy-meter` (или своё).
3. **Описание**: «Сервис учёта электроэнергии для Wiren Board» — на ваше усмотрение.
4. **Public** или **Private** — на ваше усмотрение (для open-source —
   обычно Public).
5. **НЕ** ставьте галочки «Add a README», «Add .gitignore», «Choose a
   license» — они у нас уже есть в архиве, иначе будет конфликт.
6. Нажмите **Create repository**.

GitHub покажет страницу с инструкциями. Запомните URL вашего нового
репозитория, он будет вида:

```
https://github.com/ВАШЛОГИН/wb-energy-meter.git
```

## Шаг 2. Распаковать наш репо-архив

Скачайте `wb-energy-meter-repo.tar.gz` и распакуйте его в удобное место.
В терминале:

**Linux / macOS:**
```bash
mkdir -p ~/projects
cd ~/projects
tar -xzf ~/Downloads/wb-energy-meter-repo.tar.gz
cd wb-energy-meter
```

**Windows (PowerShell):**
```powershell
mkdir C:\projects -Force
cd C:\projects
tar -xzf C:\Users\$env:USERNAME\Downloads\wb-energy-meter-repo.tar.gz
cd wb-energy-meter
```

## Шаг 3. Заменить плейсхолдер `YOURUSER` на ваш логин GitHub

В файлах `README.md`, `CHANGELOG.md`, `pyproject.toml` встречается
`YOURUSER` в URL'ах. Замените его на ваш логин GitHub.

**Linux / macOS** (одной командой):
```bash
grep -rl 'YOURUSER' . | xargs sed -i 's/YOURUSER/ВАШЛОГИН/g'
```

**Windows (PowerShell):**
```powershell
Get-ChildItem -Recurse -File | ForEach-Object {
    $content = Get-Content $_.FullName -Raw -Encoding UTF8
    if ($content -match 'YOURUSER') {
        $content -replace 'YOURUSER', 'ВАШЛОГИН' |
            Set-Content $_.FullName -Encoding UTF8 -NoNewline
    }
}
```

## Шаг 4. Инициализировать git и сделать первый коммит

```bash
git init
git add .
git status        # посмотрите, что попадёт в коммит
git commit -m "Initial release: v0.3.0

Включает:
- MQTT-клиент и парсер WB Conventions
- SQLite-реестр счётчиков
- MQTT-RPC к wb-mqtt-db и расчёт расхода
- HTTP API и CLI"
```

## Шаг 5. Привязать репозиторий и запушить

```bash
# Заменить URL на ваш, который GitHub показал в Шаге 1
git remote add origin https://github.com/ВАШЛОГИН/wb-energy-meter.git
git branch -M main
git push -u origin main
```

При первом пуше git попросит авторизацию. На GitHub давно нельзя по
паролю — нужен Personal Access Token (PAT):

1. Зайдите на https://github.com/settings/tokens
2. **Generate new token (classic)**
3. Note: `wb-energy-meter local push`
4. Expiration: 90 days (или больше)
5. Scopes: `repo` (всё под `repo`)
6. **Generate token** → скопируйте токен (он показывается **один раз**)
7. При запросе пароля в `git push` вставьте этот токен вместо пароля.

После успешного пуша обновите страницу репозитория — увидите код и README.

## Шаг 6. Расставить теги версий (опционально, но красиво)

Чтобы в Releases отображались версии и работали ссылки в CHANGELOG:

```bash
git tag -a v0.1.0 -m "v0.1.0: MQTT, статусы, in-memory реестр"
git tag -a v0.2.0 -m "v0.2.0: SQLite, миграции, CLI"
git tag -a v0.3.0 -m "v0.3.0: расчёт расхода через wb-mqtt-db RPC"
git push origin --tags
```

Дальше на GitHub можно зайти в Releases → Draft a new release → выбрать
тег → скопировать соответствующий раздел из CHANGELOG в описание.

## Шаг 7. Удалить эту инструкцию

```bash
git rm GITHUB_SETUP.md
git commit -m "docs: remove one-time setup guide"
git push
```

## Готово

Теперь:

- Каждый push в `main` будет запускать тесты в GitHub Actions
  (https://github.com/ВАШЛОГИН/wb-energy-meter/actions).
- Issues включены: https://github.com/ВАШЛОГИН/wb-energy-meter/issues
- README отображается красиво на главной странице.

## Если что-то пошло не так

**`fatal: remote origin already exists`** — у вас уже добавлен remote,
выполните `git remote set-url origin https://github.com/ВАШЛОГИН/wb-energy-meter.git`.

**`Updates were rejected because the remote contains work that you do not have locally`** —
вы не сняли галочки на Шаге 1 и GitHub создал свой README. Сделайте
`git pull --rebase origin main` и потом `git push`.

**Длинная история, нужно «сжать» всё в один коммит** — `git reset --soft
origin/main && git commit -m "..." && git push --force`. Только если вы
уверены, что репозиторий ваш и никто его не клонировал.
