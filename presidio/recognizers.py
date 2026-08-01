"""
Custom Presidio recognizers for Indian identifiers.
Phase 1: PAN + Aadhaar
"""

import re
from presidio_analyzer import Pattern, PatternRecognizer, EntityRecognizer, RecognizerResult


pan_pattern = Pattern(
    name="pan_pattern",
    regex=r"\b[A-Z]{5}[0-9]{4}[A-Z]\b",
    score=0.7,
)

pan_recognizer = PatternRecognizer(
    supported_entity="IN_PAN",
    patterns=[pan_pattern],
    context=["pan", "pan card", "permanent account number"],
)


_d = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_p = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_valid(number: str) -> bool:
    """Returns True if the digit string passes the Verhoeff checksum."""
    c = 0
    for i, item in enumerate(reversed(number)):
        c = _d[c][_p[i % 8][int(item)]]
    return c == 0


class AadhaarRecognizer(EntityRecognizer):
    PATTERN = re.compile(r"\b[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}\b")

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        results = []
        for m in self.PATTERN.finditer(text):
            raw = re.sub(r"[\s-]", "", m.group())
            score = 0.85 if verhoeff_valid(raw) else 0.3
            results.append(
                RecognizerResult(
                    entity_type="IN_AADHAAR",
                    start=m.start(),
                    end=m.end(),
                    score=score,
                )
            )
        return results


def get_analyzer():
    """Returns a Presidio AnalyzerEngine with PAN + Aadhaar recognizers added."""
    from presidio_analyzer import AnalyzerEngine

    analyzer = AnalyzerEngine()
    analyzer.registry.add_recognizer(pan_recognizer)
    analyzer.registry.add_recognizer(
        AadhaarRecognizer(supported_entities=["IN_AADHAAR"])
    )
    return analyzer


if __name__ == "__main__":
    analyzer = get_analyzer()
    text = "Mera PAN ABCDE1234F hai aur Aadhaar 2341 5678 9012 hai."
    results = analyzer.analyze(text=text, language="en")
    for r in results:
        print(f"{r.entity_type}: '{text[r.start:r.end]}' (score={r.score})")
