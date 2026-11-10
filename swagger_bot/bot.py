"""Bot CLI qui évalue l'API documentée par Swagger.

L'API doit être démarrée séparément, par exemple avec ``python api.py``.
"""

from __future__ import annotations

import argparse
import re
import json
import socket
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_MODELS = (
    "qwen2.5:1.5b",
    "llama3.2:3b",
    "bonsai-2:2b",
    "phi3:3.8b",
    "nuextract:3.8b"
)
DEFAULT_QUESTIONS = (
    "1. Quelle direction aromatique Thomas privilégie-t-il initialement, et comment son avis évolue-t-il au cours de la réunion ?",
"2. Pourquoi Sarah est-elle réticente à l'idée d'intégrer une facette boisée dans la fragrance ?",
"3. Quelles sont les trois premières pistes aromatiques proposées par Camille, et laquelle est finalement privilégiée ?",
"4. Thomas et Sarah sont-ils toujours d'accord sur le calendrier des essais ? Quelle solution Camille propose-t-elle pour résoudre leur désaccord ?",
"5. Pourquoi Sarah remet-elle en question le terme « fraîche » pour décrire la vanille ?",
"6. Quelle est la décision finale concernant le nombre d'échantillons et les tests à effectuer ?",
"7. Quel est le compromis final concernant le positionnement marketing « vanille premium » ?"
)
SUCCESS_RATINGS = (0, 50, 100)
DEFAULT_REPORT_PATH = "benchmark_report.pdf"
DEFAULT_ANSWERS_PATH = Path(__file__).resolve().parents[1] / "answers.json"


class RequestTimeoutError(RuntimeError):
    """Indique qu'un appel HTTP a dépassé le délai autorisé."""


@dataclass(frozen=True)
class QuestionResult:
    model_group: str
    question: str
    answer: str
    latency_ms: float
    model: str
    success_rating: int
    timed_out: bool = False


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float,
) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if "timeout" in detail.lower() or "timed out" in detail.lower():
            raise RequestTimeoutError(f"Timeout de l'API ({url})") from exc
        raise RuntimeError(f"HTTP {exc.code} sur {url}: {detail}") from exc
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise RequestTimeoutError(f"Timeout de l'API ({url})") from exc
        raise RuntimeError(f"API inaccessible ({url}): {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RequestTimeoutError(f"Timeout de l'API ({url})") from exc

    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Réponse JSON invalide de {url}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Réponse inattendue de {url}")
    return result


def load_questions(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return DEFAULT_QUESTIONS
    try:
        questions = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Impossible de lire les questions ({path}): {exc}") from exc
    if not isinstance(questions, list) or not questions or not all(
        isinstance(question, str) and question.strip() for question in questions
    ):
        raise RuntimeError("Le fichier de questions doit contenir une liste de textes non vides.")
    return tuple(question.strip() for question in questions)


def load_expected_answers(path: Path) -> dict[str, str]:
    try:
        answers = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Impossible de lire les réponses attendues ({path}): {exc}") from exc
    if not isinstance(answers, dict) or not answers or not all(
        isinstance(key, str) and isinstance(value, str) and value.strip()
        for key, value in answers.items()
    ):
        raise RuntimeError("Le fichier de réponses doit être un objet JSON {numéro: réponse}.")
    return {key: value.strip() for key, value in answers.items()}


def question_number(question: str) -> str:
    match = re.match(r"\s*(\d+)\s*\.", question)
    if not match:
        raise RuntimeError(f"Question sans numéro exploitable : {question}")
    return match.group(1)


def compare_answer(answer: str, expected: str) -> int:
    def normalize(text: str) -> set[str]:
        return set(re.findall(r"[a-zàâçéèêëîïôûùüÿñæœ0-9]+", text.lower()))

    answer_words = normalize(answer)
    expected_words = normalize(expected)
    if not answer_words or not expected_words:
        return 0
    overlap = len(answer_words & expected_words)
    precision = overlap / len(answer_words)
    recall = overlap / len(expected_words)
    lexical_score = (2 * precision * recall / (precision + recall)) if precision + recall else 0
    sequence_score = SequenceMatcher(None, answer.lower(), expected.lower()).ratio()
    return round(max(lexical_score, sequence_score) * 100)


def ask_question(
    api_url: str,
    question: str,
    timeout: float,
    model: str,
    success_rating: int = 0,
) -> QuestionResult:
    started = time.perf_counter()
    response = request_json(
        f"{api_url.rstrip('/')}/ask",
        method="POST",
        payload={"question": question, "model": model},
        timeout=timeout,
    )
    latency_ms = (time.perf_counter() - started) * 1000
    answer = response.get("answer")
    if not isinstance(answer, str):
        raise RuntimeError(f"Réponse sans champ 'answer' pour la question : {question}")
    response_model = response.get("model")
    if not isinstance(response_model, str) or not response_model:
        response_model = model
    return QuestionResult(model, question, answer.strip(), latency_ms, response_model, success_rating)


def create_pdf_report(
    path: Path,
    api_url: str,
    models: tuple[str, ...],
    results: list[QuestionResult],
) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise RuntimeError(
            "La génération PDF nécessite ReportLab. Installez les dépendances avec "
            "`pip install -r requirements.txt`."
        ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReportTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#40566d"),
    ))
    styles.add(ParagraphStyle(name="Cell", parent=styles["BodyText"], fontSize=8, leading=10))
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=9, leading=11))

    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title="Benchmark Swagger",
        author="Swagger Benchmark Bot",
    )
    story = [
        Paragraph("Rapport de benchmark Swagger", styles["ReportTitle"]),
        Spacer(1, 8),
        Paragraph(f"<b>Date :</b> {datetime.now().astimezone().strftime('%d/%m/%Y %H:%M:%S %z')}", styles["Small"]),
        Paragraph(f"<b>API :</b> {api_url}", styles["Small"]),
        Paragraph(f"<b>Modèles IA :</b> {', '.join(models)}", styles["Small"]),
        Paragraph(f"<b>Nombre de questions :</b> {len(results)}", styles["Small"]),
        Spacer(1, 8),
        Paragraph(
            "Ce rapport a pour but d'analyser les capacités de perception et de "
            "compréhension d'un transcript afin d'en faciliter la simplification.",
            styles["Small"],
        ),
        Spacer(1, 10),
    ]

    table_data = [[
        Paragraph("<b>#</b>", styles["Cell"]),
        Paragraph("<b>Question</b>", styles["Cell"]),
        Paragraph("<b>Réponse</b>", styles["Cell"]),
        Paragraph("<b>Latence (ms)</b>", styles["Cell"]),
        Paragraph("<b>IA</b>", styles["Cell"]),
        Paragraph("<b>Comparaison</b>", styles["Cell"]),
    ]]
    row_index = 1
    for model in models:
        table_data.append([
            Paragraph(f"<b>Résultats de {model}</b>", styles["Cell"]),
            "",
            "",
            "",
            "",
            "",
        ])
        for result in (item for item in results if item.model_group == model):
            table_data.append([
                Paragraph(str(row_index), styles["Cell"]),
                Paragraph(result.question, styles["Cell"]),
                Paragraph(result.answer or "(réponse vide)", styles["Cell"]),
                Paragraph(f"{result.latency_ms:.0f}", styles["Cell"]),
                Paragraph(result.model, styles["Cell"]),
                Paragraph(f"{result.success_rating} %", styles["Cell"]),
            ])
            row_index += 1

    table = Table(
        table_data,
        colWidths=[7 * mm, 39 * mm, 67 * mm, 22 * mm, 25 * mm, 18 * mm],
        repeatRows=1,
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbe5ef")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#40566d")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9e9e9e")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f6f9")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    data_row = 1
    for model in models:
        table.setStyle(TableStyle([
            ("SPAN", (0, data_row), (-1, data_row)),
            ("BACKGROUND", (0, data_row), (-1, data_row), colors.HexColor("#e7edf2")),
            ("LINEBELOW", (0, data_row), (-1, data_row), 0.8, colors.HexColor("#9aabba")),
        ]))
        data_row += 1 + sum(1 for result in results if result.model_group == model)
    story.extend([table, Spacer(1, 14), Paragraph("Conclusion comparative", styles["Heading2"])])
    summary = {}
    for model in models:
        model_results = [result for result in results if result.model_group == model]
        summary[model] = (
            sum(result.latency_ms for result in model_results) / len(model_results),
            sum(result.success_rating for result in model_results) / len(model_results),
        )
    conclusion_data = [[
        Paragraph("<b>IA</b>", styles["Cell"]),
        Paragraph("<b>Latence moyenne (ms)</b>", styles["Cell"]),
        Paragraph("<b>Succès moyen (%)</b>", styles["Cell"]),
    ]]
    for model, (latency, score) in summary.items():
        conclusion_data.append([
            Paragraph(model, styles["Cell"]),
            Paragraph(f"{latency:.0f}", styles["Cell"]),
            Paragraph(f"{score:.0f}", styles["Cell"]),
        ])
    conclusion = Table(conclusion_data, colWidths=[60 * mm, 55 * mm, 45 * mm], repeatRows=1)
    conclusion.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dce9df")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#48604d")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9e9e9e")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f7f3")]),
    ]))
    fastest = min(summary, key=lambda model: summary[model][0])
    best_score = max(summary, key=lambda model: summary[model][1])
    story.extend([
        conclusion,
        Spacer(1, 8),
        Paragraph(
            f"IA la plus rapide : <b>{fastest}</b> ({summary[fastest][0]:.0f} ms). "
            f"Meilleur succès : <b>{best_score}</b> ({summary[best_score][1]:.0f} %).",
            styles["Small"],
        ),
    ])
    document.build(story)


def ask_success_rating() -> int:
    while True:
        try:
            rating = int(input("\nPourcentage de succès global (0, 50 ou 100) : ").strip())
        except ValueError:
            print("Valeur invalide : choisissez 0, 50 ou 100.")
            continue
        if rating in SUCCESS_RATINGS:
            return rating
        print("Valeur invalide : choisissez 0, 50 ou 100.")


def print_progress(completed: int, total: int) -> None:
    percentage = completed * 100 // total
    print(f"Progression des tests : {percentage}% ({completed}/{total})")


def run(
    api_url: str,
    questions: tuple[str, ...],
    timeout: float,
    report_path: Path,
    models: tuple[str, ...],
    expected_answers: dict[str, str],
) -> int:
    health = request_json(f"{api_url.rstrip('/')}/health", timeout=timeout)
    print(f"Début des tests | API : {api_url} | IA : {', '.join(models)}")
    total = len(questions) * len(models)
    print_progress(0, total)

    results: list[QuestionResult] = []
    completed = 0
    for model in models:
        print(f"\n===== Tests de l'IA : {model} =====")
        for index, question in enumerate(questions, start=1):
            print(f"\nQuestion {index}/{len(questions)} : {question}")
            try:
                response = ask_question(api_url, question, timeout, model)
            except (RequestTimeoutError, TimeoutError, socket.timeout) as exc:
                response = QuestionResult(
                    model_group=model,
                    question=question,
                    answer=f"Timeout dépassé ({timeout:g} secondes)",
                    latency_ms=timeout * 1000,
                    model=model,
                    success_rating=0,
                    timed_out=True,
                )
                print(f"Timeout pour {model} : {exc}")
                print("Note automatique : 0 % ; passage à la question suivante.")
            else:
                print(f"Réponse de l'IA ({model}) : {response.answer}")
            if not response.timed_out:
                answer_key = question_number(question)
                if answer_key not in expected_answers:
                    raise RuntimeError(f"Aucune réponse de référence pour la question {answer_key}.")
                score = compare_answer(response.answer, expected_answers[answer_key])
                response = replace(response, success_rating=score)
                print(f"Comparaison avec answers.json : {score} %")
            results.append(QuestionResult(
                model_group=model,
                question=response.question,
                answer=response.answer,
                latency_ms=response.latency_ms,
                model=response.model,
                success_rating=response.success_rating,
                timed_out=response.timed_out,
            ))
            completed += 1
            print_progress(completed, total)

    create_pdf_report(report_path, api_url, models, results)
    print(f"\nBenchmark PDF créé : {report_path.resolve()}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pose des questions à l'API Swagger et mesure sa latence.")
    parser.add_argument("--url", default=DEFAULT_API_URL, help=f"URL de l'API (défaut : {DEFAULT_API_URL})")
    parser.add_argument("--timeout", type=float, default=30.0, help="Timeout HTTP en secondes (défaut : 30)")
    parser.add_argument("--questions", type=Path, help="Fichier JSON contenant une liste de questions")
    parser.add_argument(
        "--answers",
        type=Path,
        default=DEFAULT_ANSWERS_PATH,
        help=f"Réponses de référence au format JSON (défaut : {DEFAULT_ANSWERS_PATH})",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        help="Tableau de modèles Ollama, un modèle par argument",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help=f"Chemin du rapport PDF (défaut : {DEFAULT_REPORT_PATH})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.timeout <= 0:
        print("Le timeout doit être supérieur à zéro.", file=sys.stderr)
        return 2
    try:
        questions = load_questions(args.questions)
        expected_answers = load_expected_answers(args.answers)
        request_json(f"{args.url.rstrip('/')}/health", timeout=args.timeout)
        models = tuple(args.models) if args.models else DEFAULT_MODELS
        return run(args.url, questions, args.timeout, args.output, models, expected_answers)
    except (RuntimeError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("\nArrêt demandé.", file=sys.stderr)
        else:
            print(f"Erreur : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())