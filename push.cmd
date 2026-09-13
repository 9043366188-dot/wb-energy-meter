@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion
title wb-energy-meter — отправка в GitHub

set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"

echo ============================================================
echo  wb-energy-meter — отправка коммитов в GitHub
echo  Репозиторий: %REPO%
echo ============================================================
echo.

cd /d "%REPO%" || (
    echo [ОШИБКА] Не удалось перейти в каталог репозитория.
    goto :end
)

where git > nul 2>&1 || (
    echo [ОШИБКА] git не найден в PATH.
    echo Установите Git для Windows: https://git-scm.com/download/win
    goto :end
)

if not exist ".git" (
    echo [ОШИБКА] В этом каталоге нет .git — это не репозиторий.
    goto :end
)

echo [1/5] Убираю служебный мусор из .git ...
if exist ".git\_trash"            rd /s /q ".git\_trash"            2>nul
if exist ".git\_stale_lock_junk"  rd /s /q ".git\_stale_lock_junk"  2>nul
if exist ".git\index.lock"        del /f /q ".git\index.lock"       2>nul
if exist ".git\HEAD.lock"         del /f /q ".git\HEAD.lock"        2>nul
if exist ".git\objects\maintenance.lock" del /f /q ".git\objects\maintenance.lock" 2>nul
echo       готово.
echo.

echo [2/5] Незакоммиченные изменения:
git status --short
for /f %%i in ('git status --porcelain ^| find /c /v ""') do set "DIRTY=%%i"
if not "!DIRTY!"=="0" (
    echo.
    echo [ВНИМАНИЕ] В рабочем дереве есть незакоммиченные изменения — !DIRTY! шт.
    echo Они НЕ будут отправлены. Отправляются только коммиты.
    echo.
    choice /c YN /n /m "Продолжить всё равно? [Y=да, N=выйти]: "
    if errorlevel 2 goto :end
) else (
    echo       чисто.
)
echo.

echo [3/5] Забираю изменения с сервера ...
git fetch origin
if errorlevel 1 (
    echo [ОШИБКА] Не удалось связаться с GitHub. Проверьте интернет и VPN.
    goto :end
)
echo.

echo [4/5] Коммиты к отправке:
git log --oneline origin/main..HEAD
for /f %%i in ('git rev-list --count origin/main..HEAD') do set "AHEAD=%%i"
for /f %%i in ('git rev-list --count HEAD..origin/main') do set "BEHIND=%%i"

if "!AHEAD!"=="0" (
    echo       Нечего отправлять — всё уже на GitHub.
    goto :end
)
if not "!BEHIND!"=="0" (
    echo.
    echo [ВНИМАНИЕ] На сервере есть !BEHIND! коммитов, которых нет у вас.
    echo Обычный push будет отклонён. Сначала выполните: git pull --rebase origin main
    goto :end
)
echo.

echo [5/5] Отправляю !AHEAD! коммит(ов) в origin/main ...
echo.
git push origin main
if errorlevel 1 (
    echo.
    echo ============================================================
    echo  [НЕУДАЧА] Push не прошёл.
    echo.
    echo  Частые причины:
    echo   * не авторизованы — откройте GitHub Desktop и войдите,
    echo     после этого учётные данные подхватятся и здесь;
    echo   * нет доступа в интернет или мешает VPN;
    echo   * на сервере появились новые коммиты — сделайте
    echo     git pull --rebase origin main и запустите скрипт заново.
    echo ============================================================
    goto :end
)

echo.
echo ============================================================
echo  [ГОТОВО] Отправлено коммитов: !AHEAD!
echo  https://github.com/9043366188-dot/wb-energy-meter
echo ============================================================

:end
echo.
pause
endlocal
