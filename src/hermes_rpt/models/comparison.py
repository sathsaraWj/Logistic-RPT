"""Baseline comparison report (Phase 10) — "produce a comparison report and select the
strongest baseline." PR-AUC is the selection metric, not ROC-AUC or accuracy: with an imbalanced
positive class (delivery delays are the minority outcome — see docs/DATASET_BUILDING.md §6),
PR-AUC is far more sensitive to how well a model actually ranks the rare positive class, which is
exactly what "do not rely on accuracy alone" is warning against.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from hermes_rpt.models.training import TrainingResult


class ComparisonReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    results: tuple[TrainingResult, ...]
    selected_model_type: str
    selection_metric: str = "pr_auc"

    def as_markdown(self) -> str:
        header = "| Model | ROC-AUC | PR-AUC | Precision | Recall | F1 | Brier |\n"
        header += "|---|---|---|---|---|---|---|\n"
        rows = []
        for result in self.results:
            marker = " **(selected)**" if result.model_type == self.selected_model_type else ""
            m = result.metrics
            rows.append(
                f"| {result.model_type}{marker} | {m.roc_auc:.4f} | {m.pr_auc:.4f} | "
                f"{m.precision:.4f} | {m.recall:.4f} | {m.f1:.4f} | {m.brier_score:.4f} |"
            )
        return header + "\n".join(rows)


def build_comparison_report(results: list[TrainingResult]) -> ComparisonReport:
    if not results:
        raise ValueError("Cannot build a comparison report from zero training results")
    best = max(results, key=lambda r: r.metrics.pr_auc)
    return ComparisonReport(results=tuple(results), selected_model_type=best.model_type)
