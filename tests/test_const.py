from puffco_peak.const import INFO_FIELDS, POLL_FIELDS, PUSHED_FIELD_PERIODS
from puffco_peak.fields import FIELDS


def test_every_named_field_is_in_the_field_table():
    assert {*POLL_FIELDS, *INFO_FIELDS, *PUSHED_FIELD_PERIODS} <= set(FIELDS)


def test_every_pushed_field_is_also_polled():
    assert set(PUSHED_FIELD_PERIODS) <= set(POLL_FIELDS)
