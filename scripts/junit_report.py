"""Export real pytest testcase counts without losing raw subtest totals.

Pytest 9 includes successful unittest subtests in the suite summary but not as
testcase elements. Preserve the raw report and original total, while exposing
the actual exported cases to strict JUnit consumers. Never erase failures.
"""

from xml.etree import ElementTree as ET


def normalize_junit(path):
    raw = path.read_bytes()
    tree = ET.fromstring(raw)
    suites = list(tree.iter("testsuite"))
    if not suites:
        raise ValueError("No test suites exported")
    for suite in suites:
        cases = suite.findall("testcase")
        if not cases or suite.findall("testsuite"):
            raise ValueError("Expected a nonempty flat pytest suite")
        declared = int(suite.get("tests", "0"))
        if declared < len(cases):
            raise ValueError("Declared tests are fewer than exported cases")
        for kind, counter in [("failure", "failures"), ("error", "errors"), ("skipped", "skipped")]:
            actual = sum(case.find(kind) is not None for case in cases)
            if int(suite.get(counter, "0")) != actual:
                raise ValueError("Failure/error/skip counts disagree with exported cases")
        properties = suite.find("properties")
        if properties is None:
            properties = ET.SubElement(suite, "properties")
        ET.SubElement(properties, "property", name="pytest_reported_tests", value=str(declared))
        ET.SubElement(properties, "property", name="exported_testcases", value=str(len(cases)))
        suite.set("tests", str(len(cases)))
    path.with_suffix(".raw.xml").write_bytes(raw)
    ET.ElementTree(tree).write(path, encoding="utf-8", xml_declaration=True)
