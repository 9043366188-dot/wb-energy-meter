"""Определение формата и размеров PNG/JPEG без Pillow (ТЗ v0.11.0, §6).

Правило проекта: никаких новых Python-зависимостей. Ширину и высоту
картинки плана достаём из заголовков файла стандартной библиотекой —
`struct` для PNG (фиксированное смещение в чанке IHDR) и разбор
маркеров сегментов для JPEG (SOFx).

Формат определяется СТРОГО по сигнатуре первых байт файла — не по
расширению загруженного имени и не по Content-Type из запроса (их
подделать тривиально). SVG и любой другой формат не распознаются
никогда — вызывающий код (`api.py`) обязан отказать с советом
экспортировать план в PNG.
"""

from __future__ import annotations

import struct
from typing import Tuple

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"

# SOFx-маркеры, из которых можно достать размеры кадра. SOF4 (0xC4, DHT),
# SOF8 (0xC8, JPG расширение) и SOF12 (0xCC, DAC) в этот список НЕ входят —
# это не Start-Of-Frame маркеры, у них другое назначение сегмента.
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
# Маркеры без сегмента длины (standalone) — после них сразу следующий байт.
_JPEG_STANDALONE = frozenset({0x01, 0xD8, 0xD9}) | set(range(0xD0, 0xD8))


class ImageFormatError(ValueError):
    """Файл не PNG/JPEG, либо его заголовок повреждён/не разобрать.
    Текст — по-русски, годится для прямого показа пользователю."""


def detect_image_format(data: bytes) -> str:
    """"png" | "jpeg" по сигнатуре первых байт. Больше ничего не
    принимается (в частности, SVG — сознательно, см. §6, §12 ТЗ)."""
    if data[:8] == PNG_SIGNATURE:
        return "png"
    if data[:3] == JPEG_SIGNATURE:
        return "jpeg"
    raise ImageFormatError(
        "Неизвестный формат файла — принимаются только PNG и JPEG "
        "(проверено по сигнатуре, не по расширению). Если план в другом "
        "формате (например, SVG или DWG), экспортируйте его в PNG.")


def parse_png_size(data: bytes) -> Tuple[int, int]:
    """(width, height) из чанка IHDR. IHDR — первый чанк, всегда сразу
    после 8-байтовой сигнатуры: 4 байта длины, 4 байта типа "IHDR",
    затем 4+4 байта width/height big-endian (спецификация PNG)."""
    if len(data) < 24 or data[:8] != PNG_SIGNATURE:
        raise ImageFormatError("Файл повреждён: это не PNG (нет сигнатуры)")
    if data[12:16] != b"IHDR":
        raise ImageFormatError(
            "Файл повреждён: в PNG не найден чанк IHDR на ожидаемом месте")
    try:
        width, height = struct.unpack(">II", data[16:24])
    except struct.error as e:
        raise ImageFormatError(f"Не удалось разобрать размеры PNG: {e}")
    if width <= 0 or height <= 0:
        raise ImageFormatError(
            f"PNG сообщает нулевой или отрицательный размер: {width}x{height}")
    return width, height


def parse_jpeg_size(data: bytes) -> Tuple[int, int]:
    """(width, height) — проход по маркерам сегментов JPEG до первого
    SOF0–SOF3/SOF5–SOF7/SOF9–SOF11/SOF13–SOF15."""
    n = len(data)
    if n < 4 or data[:2] != b"\xff\xd8":
        raise ImageFormatError("Файл повреждён: это не JPEG (нет сигнатуры)")
    i = 2
    while i < n - 1:
        if data[i] != 0xFF:
            # Не на маркере — досинхронизируемся побайтово.
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            # Байты-заполнители перед маркером — пропускаем.
            i += 1
            continue
        i += 2
        if marker in _JPEG_STANDALONE:
            continue
        if i + 2 > n:
            break
        seg_len = struct.unpack(">H", data[i:i + 2])[0]
        if marker in _JPEG_SOF_MARKERS:
            if i + 7 > n or seg_len < 7:
                raise ImageFormatError(
                    "Файл повреждён: сегмент SOF в JPEG обрезан")
            height = struct.unpack(">H", data[i + 3:i + 5])[0]
            width = struct.unpack(">H", data[i + 5:i + 7])[0]
            if width <= 0 or height <= 0:
                raise ImageFormatError(
                    f"JPEG сообщает нулевой или отрицательный размер: "
                    f"{width}x{height}")
            return width, height
        if seg_len < 2:
            raise ImageFormatError(
                "Файл повреждён: некорректная длина сегмента JPEG")
        i += seg_len
    raise ImageFormatError(
        "Не удалось определить размеры JPEG — SOF-маркер не найден "
        "(файл повреждён или это не полноценный JPEG)")


def parse_image_size(data: bytes) -> Tuple[str, int, int]:
    """(format, width, height). format — "png" | "jpeg".

    Формат определяется по сигнатуре, размеры — из заголовков.
    Любая проблема с разбором -> ImageFormatError с русским текстом;
    вызывающий код обязан не сохранять файл в этом случае (§6 ТЗ)."""
    fmt = detect_image_format(data)
    if fmt == "png":
        width, height = parse_png_size(data)
    else:
        width, height = parse_jpeg_size(data)
    return fmt, width, height
