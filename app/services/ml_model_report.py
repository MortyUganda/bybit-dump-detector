"""
Парсер артефактов обучения (`models/model_txt_*.txt`).

Используется кнопкой «🤖 Данные ИИ-модели» в /ml_short → Настройки.

Логика:
- Берём самый свежий model_txt БЕЗ суффиксов (это prod-decision).
  Файлы `*_v2.txt` и `*_nodead.txt` — эксперименты, игнорируем.
- Парсим: AUC mean±std, folds, n сигналов, WR по источникам, число фич,
  таблицу ML-фильтра OOF.
- Для сравнения берём предыдущий по mtime model_txt того же типа.
- Рекомендуем порог по эвристике max(WR × √n) — баланс качества и числа сделок.

Все парсеры терпимы к отсутствию полей: возвращают None вместо падения.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


MODELS_DIR = Path("models")


@dataclass
class ThresholdRow:
    threshold: float
    n: int
    share_pct: float
    wr_pct: float
    delta_pct: float


@dataclass
class ModelReport:
    path: Path
    mtime: datetime
    # Заголовок
    auto_n: int | None = None
    canceled_n: int | None = None
    auto_wr: float | None = None
    canceled_wr: float | None = None
    total_n: int | None = None
    total_wr: float | None = None
    features_count: int | None = None
    # AUC
    mean_auc: float | None = None
    std_auc: float | None = None
    folds: list[float] = field(default_factory=list)
    # ML-фильтр
    thresholds: list[ThresholdRow] = field(default_factory=list)
    # Модель
    model_file: str | None = None
    saved_at: datetime | None = None


# ── Регулярные выражения ────────────────────────────────────────────

_RE_AUTO_WR = re.compile(r"auto_short\s+n=(\d+),\s*WR=([\d.]+)%")
_RE_CANCEL_WR = re.compile(r"canceled\s+n=(\d+),\s*WR=([\d.]+)%")
_RE_TOTAL = re.compile(r"Объединённый датасет:\s*(\d+)\s*сигналов")
_RE_TOTAL_WR = re.compile(r"WR общий:\s*([\d.]+)%")
_RE_FEATURES = re.compile(r"Итого фичей:\s*(\d+)")
_RE_MEAN_AUC = re.compile(r"Средний AUC:\s*([\d.]+)\s*±\s*([\d.]+)")
_RE_FOLD = re.compile(r"Fold \d+:.*?AUC=([\d.]+)")
_RE_THRESHOLD = re.compile(
    r"proba>=([\d.]+):\s*n=\s*(\d+)\s*\(\s*([\d.]+)%\),\s*WR=([\d.]+)%\s*\(Δ=([+\-][\d.]+)%\)"
)
_RE_MODEL_FILE = re.compile(r"Модель сохранена:\s*([^\s]+\.pkl)")
_RE_STAMP_IN_NAME = re.compile(r"_(\d{4}-\d{2}-\d{2})_(\d{6})")


def _parse_one(path: Path) -> ModelReport:
    """Парсит один model_txt; устойчив к отсутствию любых полей."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ModelReport(path=path, mtime=datetime.fromtimestamp(0, timezone.utc))

    rep = ModelReport(
        path=path,
        mtime=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc),
    )

    if m := _RE_AUTO_WR.search(text):
        rep.auto_n = int(m.group(1))
        rep.auto_wr = float(m.group(2))
    if m := _RE_CANCEL_WR.search(text):
        rep.canceled_n = int(m.group(1))
        rep.canceled_wr = float(m.group(2))
    if m := _RE_TOTAL.search(text):
        rep.total_n = int(m.group(1))
    if m := _RE_TOTAL_WR.search(text):
        rep.total_wr = float(m.group(1))
    if m := _RE_FEATURES.search(text):
        rep.features_count = int(m.group(1))
    if m := _RE_MEAN_AUC.search(text):
        rep.mean_auc = float(m.group(1))
        rep.std_auc = float(m.group(2))
    rep.folds = [float(x) for x in _RE_FOLD.findall(text)]

    for tm in _RE_THRESHOLD.finditer(text):
        rep.thresholds.append(ThresholdRow(
            threshold=float(tm.group(1)),
            n=int(tm.group(2)),
            share_pct=float(tm.group(3)),
            wr_pct=float(tm.group(4)),
            delta_pct=float(tm.group(5)),
        ))

    if m := _RE_MODEL_FILE.search(text):
        rep.model_file = m.group(1).split("\\")[-1].split("/")[-1]
        if sm := _RE_STAMP_IN_NAME.search(rep.model_file):
            try:
                # stamp в имени .pkl — локальное время компьютера, оставляем naive,
                # _effective_ref потом интерпретирует как local-time.
                rep.saved_at = datetime.strptime(
                    f"{sm.group(1)} {sm.group(2)}",
                    "%Y-%m-%d %H%M%S",
                )
            except ValueError:
                pass

    return rep


def _list_prod_txt(models_dir: Path = MODELS_DIR) -> list[Path]:
    """
    Возвращает model_txt prod-decision (без суффиксов _v2/_nodead),
    отсортированные по mtime убыванию.
    """
    if not models_dir.exists():
        return []
    files = []
    for p in models_dir.glob("model_txt_*.txt"):
        # Отсекаем эксперименты
        stem = p.stem  # model_txt_2026-05-10_202504
        # Эксперимент если в имени есть _v2 или _nodead перед расширением
        if stem.endswith("_v2") or stem.endswith("_nodead"):
            continue
        files.append(p)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def get_current_and_previous(
    models_dir: Path = MODELS_DIR,
) -> tuple[ModelReport | None, ModelReport | None]:
    """Возвращает (current, previous) — оба могут быть None."""
    files = _list_prod_txt(models_dir)
    current = _parse_one(files[0]) if len(files) >= 1 else None
    previous = _parse_one(files[1]) if len(files) >= 2 else None
    return current, previous


def _find_txt_for_pkl(pkl_name: str, models_dir: Path = MODELS_DIR) -> Path | None:
    """
    По имени .pkl ищет соответствующий model_txt_*.txt.
    Имена синхронизированы: decision_model_<date>_<HHMMSS>.pkl ↔ model_txt_<date>_<HHMMSS>.txt
    """
    if not pkl_name:
        return None
    stem = pkl_name.replace(".pkl", "")
    # decision_model_2026-05-10_202504 → model_txt_2026-05-10_202504
    if stem.startswith("decision_model_"):
        suffix = stem[len("decision_model_"):]
        candidate = models_dir / f"model_txt_{suffix}.txt"
        if candidate.exists():
            return candidate
    # legacy decision_model.pkl — берём самый свежий model_txt
    return None


def get_loaded_vs_disk(
    loaded_pkl_path: Path | None,
    models_dir: Path = MODELS_DIR,
) -> tuple[ModelReport | None, ModelReport | None, bool]:
    """
    Возвращает (loaded_report, disk_report, is_same).

    - loaded_report — отчёт по модели, которая сейчас в памяти у analyzer.
    - disk_report — отчёт по самому свежему prod model_txt на диске.
    - is_same — True если оба указывают на одну и ту же модель (нет смысла перезагружать).
    """
    files = _list_prod_txt(models_dir)
    disk_report = _parse_one(files[0]) if files else None

    loaded_report: ModelReport | None = None
    if loaded_pkl_path is not None:
        txt = _find_txt_for_pkl(loaded_pkl_path.name, models_dir)
        if txt is not None:
            loaded_report = _parse_one(txt)

    # Если loaded не нашли — fallback на previous (пред. prod)
    if loaded_report is None and len(files) >= 2:
        loaded_report = _parse_one(files[1])

    is_same = False
    if loaded_report is not None and disk_report is not None:
        is_same = loaded_report.path == disk_report.path

    return loaded_report, disk_report, is_same


def render_compare(
    loaded: ModelReport | None,
    disk: ModelReport | None,
    loaded_pkl_path: Path | None,
) -> str:
    """HTML-сравнение текущей (загруженной) vs новой (на диске) модели."""
    if disk is None:
        return (
            "♻️ <b>Перезагрузка модели</b>\n\n"
            "<i>На диске нет моделей.</i> Сначала обучи:\n"
            "<code>.\\scripts\\run_ml.ps1 -Mode decision -Txt</code>"
        )

    def _fmt(rep: ModelReport | None, label: str) -> list[str]:
        if rep is None:
            return [f"<b>{label}:</b> <i>нет данных</i>"]
        date_str, age = _format_age(rep.saved_at, rep.mtime)
        lines = [f"<b>{label}</b> ({age})"]
        if rep.model_file:
            lines.append(f"  📦 <code>{rep.model_file}</code>")
        lines.append(f"  📅 {date_str}")
        if rep.mean_auc is not None:
            auc = f"{rep.mean_auc:.3f}"
            if rep.std_auc is not None:
                auc += f" ± {rep.std_auc:.3f}"
            lines.append(f"  📈 AUC: <b>{auc}</b>")
        if rep.total_n is not None and rep.total_wr is not None:
            lines.append(f"  📊 n={rep.total_n}, WR={rep.total_wr:.1f}%")
        if rep.features_count is not None:
            lines.append(f"  🎯 фич: {rep.features_count}")
        rec = recommend_threshold(rep)
        if rec is not None:
            lines.append(
                f"  💡 рек.порог {rec.threshold:.2f}: n={rec.n}, WR={rec.wr_pct:.1f}%"
            )
        return lines

    parts = ["♻️ <b>Сравнение моделей</b>", ""]
    parts.extend(_fmt(loaded, "⚙️ В памяти"))
    parts.append("")
    parts.extend(_fmt(disk, "🆕 На диске"))

    # Diff блок
    if (
        loaded is not None
        and disk is not None
        and loaded.mean_auc is not None
        and disk.mean_auc is not None
    ):
        d_auc = disk.mean_auc - loaded.mean_auc
        sign = "+" if d_auc >= 0 else ""
        parts.append("")
        parts.append(f"📐 <b>Δ AUC: {sign}{d_auc:.3f}</b>")
        if loaded.total_n is not None and disk.total_n is not None:
            dn = disk.total_n - loaded.total_n
            sign_n = "+" if dn >= 0 else ""
            parts.append(f"📐 <b>Δ n: {sign_n}{dn}</b>")

    if loaded_pkl_path is None:
        parts.append("")
        parts.append(
            "<i>⚠️ Путь к загруженной модели неизвестен (analyzer ещё не "
            "использовал ML-gate). Сверху показана предыдущая prod-модель.</i>"
        )

    return "\n".join(parts)


def recommend_threshold(rep: ModelReport) -> ThresholdRow | None:
    """
    Эвристика max(Δ × √n): лифт над baseline взвешенный по числу сделок.

    Это аналог z-статистики: выбираем порог, где лифт (WR над baseline)
    максимален с учётом статистической значимости (√n).

    Доп. ограничения:
    - n >= 50 (иначе слишком мало для уверенных выводов)
    - Δ > 0 (пороги хуже baseline сразу отбрасываем)
    """
    if not rep.thresholds:
        return None
    eligible = [t for t in rep.thresholds if t.n >= 50 and t.delta_pct > 0]
    if not eligible:
        return None
    return max(eligible, key=lambda t: t.delta_pct * math.sqrt(t.n))


# ── Рендер ──────────────────────────────────────────────────────────

def _effective_ref(saved_at: datetime | None, mtime: datetime) -> datetime:
    """
    Возвращает время обучения в local-tz. saved_at приходит наивным (stamp из имени .pkl,
    это локальное время компьютера) — привязываем к local. Fallback — mtime файла (в UTC).
    """
    if saved_at is not None:
        if saved_at.tzinfo is None:
            return saved_at.astimezone()
        return saved_at
    return mtime


def _format_age(saved_at: datetime | None, mtime: datetime) -> tuple[str, str]:
    """Возвращает (строка_даты, строка_возраста)."""
    ref = _effective_ref(saved_at, mtime)
    now = datetime.now(ref.tzinfo) if ref.tzinfo else datetime.now()
    secs = (now - ref).total_seconds()
    total_h = secs / 3600
    if secs < 0:
        age_str = "только что"
    elif total_h < 1:
        age_str = f"{int(secs / 60)} мин назад"
    elif total_h < 48:
        age_str = f"{total_h:.1f} ч назад"
    else:
        age_str = f"{total_h / 24:.1f} д назад"
    date_str = ref.strftime("%d.%m.%Y %H:%M")
    return date_str, age_str


def _age_warning(saved_at: datetime | None, mtime: datetime) -> str:
    """Предупреждение если модель устарела."""
    ref = _effective_ref(saved_at, mtime)
    now = datetime.now(ref.tzinfo) if ref.tzinfo else datetime.now()
    age_h = max(0.0, (now - ref).total_seconds() / 3600)
    if age_h > 72:
        return "🔴 <b>Модель устарела (>3 дней)</b>, срочно переобучи."
    if age_h > 24:
        return "🟡 <b>Модель старше суток</b>, рекомендуется переобучить."
    return ""


def _format_thresholds(
    current: list[ThresholdRow],
    previous: list[ThresholdRow] | None,
    cur_threshold: float | None,
    recommended: ThresholdRow | None,
) -> str:
    """Таблица порогов с подсветкой текущего и рекомендованного, и Δ vs prev."""
    if not current:
        return "<i>нет данных</i>"
    prev_map = {t.threshold: t for t in (previous or [])}
    lines = []
    for t in current:
        marker = ""
        if recommended and abs(t.threshold - recommended.threshold) < 1e-6:
            marker += " ⭐"
        if cur_threshold is not None and abs(t.threshold - cur_threshold) < 1e-6:
            marker += " ◀️"
        delta_prev = ""
        if t.threshold in prev_map:
            d = t.wr_pct - prev_map[t.threshold].wr_pct
            sign = "+" if d >= 0 else ""
            delta_prev = f" <i>(Δ vs prev: {sign}{d:.1f}pp)</i>"
        lines.append(
            f"  ≥{t.threshold:.2f}: n=<b>{t.n}</b> ({t.share_pct:.1f}%) "
            f"WR=<b>{t.wr_pct:.1f}%</b>{delta_prev}{marker}"
        )
    return "\n".join(lines)


def render_model_info(
    current: ModelReport | None,
    previous: ModelReport | None,
    cur_threshold: float | None,
) -> str:
    """Финальный HTML-текст для Telegram."""
    if current is None:
        return (
            "🤖 <b>Данные ИИ-модели</b>\n\n"
            "<i>Нет файлов models/model_txt_*.txt.</i>\n"
            "Запусти обучение с флагом -Txt:\n"
            "<code>.\\scripts\\run_ml.ps1 -Mode decision -Txt</code>"
        )

    date_str, age_str = _format_age(current.saved_at, current.mtime)
    warning = _age_warning(current.saved_at, current.mtime)

    # AUC + Δ
    auc_line = "<i>нет</i>"
    if current.mean_auc is not None:
        auc_line = f"<b>{current.mean_auc:.3f}</b>"
        if current.std_auc is not None:
            auc_line += f" ± {current.std_auc:.3f}"
        if previous and previous.mean_auc is not None:
            d = current.mean_auc - previous.mean_auc
            sign = "+" if d >= 0 else ""
            auc_line += f" <i>(prev: {previous.mean_auc:.3f}, Δ {sign}{d:.3f})</i>"

    folds_line = ""
    if current.folds:
        folds_line = "  Folds: " + " / ".join(f"{f:.3f}" for f in current.folds)

    # Сигналы
    sig_lines = []
    if current.total_n is not None and current.total_wr is not None:
        sig_lines.append(f"📊 Сигналов: <b>{current.total_n}</b> (WR={current.total_wr:.1f}%)")
    if current.auto_n is not None:
        sig_lines.append(f"  • auto_short: {current.auto_n} (WR={current.auto_wr:.1f}%)")
    if current.canceled_n is not None:
        sig_lines.append(f"  • canceled:   {current.canceled_n} (WR={current.canceled_wr:.1f}%)")
    if current.features_count is not None:
        sig_lines.append(f"🎯 Фичей: <b>{current.features_count}</b>")
    if previous and previous.total_n is not None and current.total_n is not None:
        d = current.total_n - previous.total_n
        sign = "+" if d >= 0 else ""
        sig_lines.append(f"  <i>(prev: n={previous.total_n}, Δ {sign}{d})</i>")

    # Рекомендация
    rec = recommend_threshold(current)
    rec_line = ""
    if rec is not None:
        cur_str = f" (сейчас {cur_threshold:.2f})" if cur_threshold is not None else ""
        rec_line = (
            f"\n💡 <b>Рекомендуемый порог: {rec.threshold:.2f}</b>{cur_str}\n"
            f"   n={rec.n}, WR={rec.wr_pct:.1f}%, лифт взвешенный по √n: {rec.delta_pct * math.sqrt(rec.n):.0f}"
        )

    parts = [
        "🤖 <b>Данные ИИ-модели</b>",
        "",
        f"📅 Обучена: <b>{date_str}</b> ({age_str})",
    ]
    if current.model_file:
        parts.append(f"📦 <code>{current.model_file}</code>")
    if warning:
        parts.append(warning)
    parts.append("")
    parts.extend(sig_lines)
    parts.append("")
    parts.append(f"📈 AUC: {auc_line}")
    if folds_line:
        parts.append(folds_line)
    parts.append("")
    parts.append("🎚 <b>ML-фильтр (OOF)</b>:")
    parts.append(_format_thresholds(
        current.thresholds,
        previous.thresholds if previous else None,
        cur_threshold,
        rec,
    ))
    if rec_line:
        parts.append(rec_line)
    if previous:
        prev_date, prev_age = _format_age(previous.saved_at, previous.mtime)
        parts.append("")
        parts.append(f"<i>Сравнение с предыдущей: {prev_date} ({prev_age})</i>")

    return "\n".join(parts)
