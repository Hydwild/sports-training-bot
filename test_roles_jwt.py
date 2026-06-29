"""График посещаемости (PNG в память) через matplotlib."""
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def attendance_chart_png(ranking: list[dict]) -> bytes | None:
    """Принимает готовый рейтинг [{name, attended, hours}]. Возвращает PNG или None."""
    if not ranking:
        return None
    data = list(reversed(ranking))
    names = [d["name"] for d in data]
    counts = [d["attended"] for d in data]

    fig, ax = plt.subplots(figsize=(8, max(2.5, 0.5 * len(data) + 1)))
    bars = ax.barh(names, counts, color="#3a7bd5")
    ax.set_xlabel("Посещено тренировок")
    ax.set_title("Посещаемость участников")
    ax.bar_label(bars, padding=3)
    ax.xaxis.get_major_locator().set_params(integer=True)
    ax.margins(x=0.1)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
