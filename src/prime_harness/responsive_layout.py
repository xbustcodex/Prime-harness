"""Responsive window and splitter geometry for the Prime Harness desktop app.

All functions work in Qt logical pixels. Qt reports ``availableGeometry()``
for a screen in logical pixels, already excluding the Windows taskbar, and it
applies display scaling itself, so these helpers never need raw device pixels.
They take plain rectangles so they can be tested at any resolution.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize

PREFERRED_WINDOW = QSize(1400, 900)
# Fraction of the usable area used at first launch, leaving a visible margin.
STARTUP_WIDTH_RATIO = 0.96
STARTUP_HEIGHT_RATIO = 0.94


def initial_window_rect(available: QRect, preferred: QSize = PREFERRED_WINDOW) -> QRect:
    """Largest comfortable rectangle at most ``preferred``, centred in ``available``."""
    width = max(1, min(preferred.width(), int(available.width() * STARTUP_WIDTH_RATIO)))
    height = max(1, min(preferred.height(), int(available.height() * STARTUP_HEIGHT_RATIO)))
    x = available.x() + (available.width() - width) // 2
    y = available.y() + (available.height() - height) // 2
    return QRect(x, y, width, height)


def clamp_window_rect(saved: QRect, available: QRect, minimum: QSize) -> QRect:
    """Fit a restored rectangle inside the current usable area.

    The size is reduced to the available area when the display or scaling
    shrank, and the position is moved so the title bar stays on screen. The
    minimum is honoured only when the screen is large enough to hold it.
    """
    width = min(max(saved.width(), min(minimum.width(), available.width())), available.width())
    height = min(max(saved.height(), min(minimum.height(), available.height())), available.height())
    max_x = available.x() + available.width() - width
    max_y = available.y() + available.height() - height
    x = min(max(saved.x(), available.x()), max_x)
    y = min(max(saved.y(), available.y()), max_y)
    return QRect(x, y, width, height)


def rect_fits(rect: QRect, available: QRect) -> bool:
    return (
        rect.x() >= available.x()
        and rect.y() >= available.y()
        and rect.right() <= available.right()
        and rect.bottom() <= available.bottom()
    )


def fractions_from_sizes(sizes: list[int]) -> list[float]:
    """Relative share of each splitter pane; used to persist layouts across resolutions."""
    if not sizes:
        return []
    total = sum(max(0, size) for size in sizes)
    if total <= 0:
        return [1.0 / len(sizes)] * len(sizes)
    return [max(0, size) / total for size in sizes]


def sizes_from_fractions(
    fractions: list[float], total: int, minimums: list[int] | None = None
) -> list[int]:
    """Pixel sizes that keep the saved proportions and sum exactly to ``total``.

    Panes are raised to their minimums first; the excess is then taken from the
    largest panes so the arrangement stays as close to the saved proportions as
    the minimums allow. If the minimums alone exceed ``total``, they are returned
    unchanged and the enclosing scroll area provides access.
    """
    if not fractions:
        return []
    count = len(fractions)
    floors = list(minimums) if minimums is not None else [0] * count
    if len(floors) != count:
        raise ValueError("minimums must match fractions in length")
    if total <= 0:
        return floors
    weights = [max(0.0, value) for value in fractions]
    weight_sum = sum(weights) or float(count)
    if sum(weights) == 0:
        weights = [1.0] * count
    sizes = [int(total * weight / weight_sum) for weight in weights]
    # Rounding leftovers go to the largest pane, so a hidden (zero-weight) pane stays empty.
    largest = max(range(count), key=lambda i: weights[i])
    sizes[largest] += total - sum(sizes)
    sizes = [max(floor, size) for floor, size in zip(floors, sizes, strict=True)]
    if sum(floors) >= total:
        return floors
    excess = sum(sizes) - total
    while excess > 0:
        index = max(range(count), key=lambda i: sizes[i] - floors[i])
        spare = sizes[index] - floors[index]
        if spare <= 0:
            break
        take = min(spare, excess)
        sizes[index] -= take
        excess -= take
    return sizes
