"""
Accuracy test harness for PAN + Aadhaar recognizers.

Run:
    python3 test_recognizers.py

Each test case has an `expect` field — what we WANT to be detected.
Script prints PASS/FAIL for each case so you can spot false positives
and false negatives quickly.
"""

from recognizers import get_analyzer

analyzer = get_analyzer()

# ---------------------------------------------------------------------
# Test cases
# expect: list of entity_types that SHOULD be found (order doesn't matter)
# ---------------------------------------------------------------------

test_cases = [
    {
        "name": "Simple PAN in sentence",
        "text": "Mera PAN number ABCDE1234F hai.",
        "expect": ["IN_PAN"],
    },
    {
        "name": "Simple Aadhaar in sentence",
        "text": "Aadhaar card number 234123456789 hai.",
        "expect": ["IN_AADHAAR"],
    },
    {
        "name": "PAN + Aadhaar together (form-style)",
        "text": "Name: Rohan Sharma, PAN: BCDEF2345G, Aadhaar: 456712345678",
        "expect": ["IN_PAN", "IN_AADHAAR"],
    },
    {
        "name": "Aadhaar with spaces (common formatting)",
        "text": "UID: 2341 5678 9018",
        "expect": ["IN_AADHAAR"],
    },
    {
        "name": "Aadhaar with hyphens",
        "text": "UID: 2341-5678-9018",
        "expect": ["IN_AADHAAR"],
    },
    {
        "name": "False positive check — random 10-char string (not PAN format)",
        "text": "Order ID: XYZ1234567890",
        "expect": [],
    },
    {
        "name": "False positive check — phone number (10 digits, not 12)",
        "text": "Call me at 9876543210",
        "expect": [],
    },
    {
        "name": "Edge case — Aadhaar starting with 0 or 1 (invalid, should NOT match)",
        "text": "Reference number 012345678901 for tracking",
        "expect": [],
    },
    {
        "name": "Two PANs in one text",
        "text": "Buyer PAN ABCDE1234F, Seller PAN FGHIJ5678K",
        "expect": ["IN_PAN", "IN_PAN"],
    },
    {
        "name": "No PII at all",
        "text": "The weather today is nice and sunny.",
        "expect": [],
    },
]


def run_tests():
    passed = 0
    failed = 0

    for case in test_cases:
        results = analyzer.analyze(text=case["text"], language="en")

        # Only look at our custom entity types for this test harness
        found_types = sorted(
            [r.entity_type for r in results if r.entity_type in ("IN_PAN", "IN_AADHAAR")]
        )
        expected_types = sorted(case["expect"])

        status = "PASS" if found_types == expected_types else "FAIL"
        if status == "PASS":
            passed += 1
        else:
            failed += 1

        print(f"[{status}] {case['name']}")
        print(f"       text: {case['text']}")
        print(f"       expected: {expected_types}")
        print(f"       found:    {found_types}")
        if status == "FAIL":
            details = [
                f"{r.entity_type}='{case['text'][r.start:r.end]}'(score={round(r.score,2)})"
                for r in results
                if r.entity_type in ("IN_PAN", "IN_AADHAAR")
            ]
            print(f"       details:  {details}")
        print()

    print(f"----- {passed} passed, {failed} failed out of {len(test_cases)} -----")


if __name__ == "__main__":
    run_tests()
