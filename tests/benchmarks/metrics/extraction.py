"""Extraction quality metrics for SubconsciousMemory benchmarks.

Measures how well the extraction pipeline identifies meaningful
sentences versus noise. Uses ground-truth sentence labels from
fixture data.
"""

from typing import Dict, List


def extraction_f1(
    extracted: List[str],
    ground_truth: List[Dict],
    min_overlap: float = 0.5,
) -> Dict[str, float]:
    """Compute extraction precision, recall, and F1 score.

    Compares extracted sentences against ground-truth labeled sentences.
    A match is counted when an extracted sentence overlaps sufficiently
    with a ground-truth "meaningful" sentence.

    Args:
        extracted: List of extracted sentence strings.
        ground_truth: List of dicts with "text" and "label" keys.
            Labels are "meaningful" or "noise".
        min_overlap: Minimum word overlap ratio to consider a match.
            Default 0.5 (50% of words must overlap).

    Returns:
        Dict with keys "precision", "recall", "f1", and counts
        "true_positives", "false_positives", "false_negatives".
    """
    meaningful = [s for s in ground_truth if s["label"] == "meaningful"]
    noise = [s for s in ground_truth if s["label"] == "noise"]

    if not extracted:
        return {
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "true_positives": 0,
            "false_positives": 0,
            "false_negatives": len(meaningful),
        }

    # Match extracted sentences to ground truth
    matched_meaningful = set()
    matched_noise = set()

    for ext in extracted:
        ext_words = set(ext.lower().split())
        best_match_idx = -1
        best_overlap = 0.0

        for i, gt in enumerate(meaningful):
            if i in matched_meaningful:
                continue
            gt_words = set(gt["text"].lower().split())
            if not gt_words:
                continue
            overlap = len(ext_words & gt_words) / max(len(gt_words), 1)
            if overlap > best_overlap:
                best_overlap = overlap
                best_match_idx = i

        if best_match_idx >= 0 and best_overlap >= min_overlap:
            matched_meaningful.add(best_match_idx)
        else:
            # Check if it matches noise
            for i, gt in enumerate(noise):
                gt_words = set(gt["text"].lower().split())
                if not gt_words:
                    continue
                overlap = len(ext_words & gt_words) / max(len(gt_words), 1)
                if overlap >= min_overlap:
                    matched_noise.add(i)
                    break

    true_positives = len(matched_meaningful)
    false_positives = len(extracted) - true_positives
    false_negatives = len(meaningful) - true_positives

    precision = true_positives / max(len(extracted), 1)
    recall = true_positives / max(len(meaningful), 1)
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
    }
