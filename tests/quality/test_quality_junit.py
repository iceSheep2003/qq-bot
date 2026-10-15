"""Report export compatibility must preserve actual runner failures."""

from xml.etree import ElementTree as ET

import pytest

from tools.quality.junit_report import normalize_junit


def report(tmp_path, xml):
    path = tmp_path / "regression.xml"
    path.write_text(xml)
    return path


def test_subtest_summary_is_exported_with_actual_top_level_cases_and_raw_copy(tmp_path):
    xml = '<testsuites><testsuite tests="5" failures="0" errors="0" skipped="0"><testcase name="a"/><testcase name="b"/></testsuite></testsuites>'
    path = report(tmp_path, xml)
    normalize_junit(path)
    suite = ET.parse(path).getroot().find("testsuite")
    assert suite.get("tests") == "2"
    assert path.with_suffix(".raw.xml").read_text() == xml
    properties = {p.get("name"): p.get("value") for p in suite.find("properties")}
    assert properties["pytest_reported_tests"] == "5"
    assert properties["exported_testcases"] == "2"


def test_failures_errors_and_skips_are_preserved(tmp_path):
    path = report(tmp_path, '<testsuites><testsuite tests="7" failures="1" errors="1" skipped="1"><testcase><failure>assert false</failure></testcase><testcase><error>setup failed</error></testcase><testcase><skipped/></testcase></testsuite></testsuites>')
    normalize_junit(path)
    suite = ET.parse(path).getroot().find("testsuite")
    assert suite.get("tests") == "3"
    assert (suite.get("failures"), suite.get("errors"), suite.get("skipped")) == ("1", "1", "1")
    assert len(list(suite.iter("failure"))) == len(list(suite.iter("error"))) == 1


@pytest.mark.parametrize("xml", [
    '<testsuites><testsuite tests="0" failures="0" errors="0" skipped="0"><testcase/></testsuite></testsuites>',
    '<testsuites><testsuite tests="1" failures="1" errors="0" skipped="0"><testcase/></testsuite></testsuites>',
    '<testsuites><testsuite tests="0" failures="0" errors="0" skipped="0"/></testsuites>',
])
def test_malformed_counts_and_empty_suites_fail_closed(tmp_path, xml):
    with pytest.raises(ValueError):
        normalize_junit(report(tmp_path, xml))
