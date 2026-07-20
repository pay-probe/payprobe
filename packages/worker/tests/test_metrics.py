"""Zero-dependency Prometheus metrics module."""

import pytest

from worker.engine import metrics as m


def test_counter_increments_and_renders():
    c = m.Counter("pp_test_total", "help", ["code"])
    c.inc(code="200")
    c.inc(2, code="200")
    c.inc(code="500")
    text = c.render()
    assert "# TYPE pp_test_total counter" in text
    assert 'pp_test_total{code="200"} 3' in text
    assert 'pp_test_total{code="500"} 1' in text


def test_gauge_set_inc_dec():
    g = m.Gauge("pp_test_inflight", "help")
    g.inc()
    g.inc()
    g.dec()
    assert "pp_test_inflight 1" in g.render()
    g.set(5)
    assert "pp_test_inflight 5" in g.render()


def test_gauge_remove_drops_series():
    g = m.Gauge("pp_test_byrun", "help", ["run"])
    g.set(10, run="a")
    g.set(20, run="b")
    g.remove(run="a")
    text = g.render()
    assert 'run="a"' not in text
    assert 'pp_test_byrun{run="b"} 20' in text


def test_histogram_buckets_sum_count():
    h = m.Histogram("pp_test_seconds", "help", buckets=(0.1, 0.5, 1.0))
    for v in (0.05, 0.2, 0.7, 3.0):
        h.observe(v)
    text = h.render()
    assert 'pp_test_seconds_bucket{le="0.1"} 1' in text  # 0.05
    assert 'pp_test_seconds_bucket{le="0.5"} 2' in text  # 0.05, 0.2
    assert 'pp_test_seconds_bucket{le="1"} 3' in text  # +0.7
    assert 'pp_test_seconds_bucket{le="+Inf"} 4' in text  # all
    assert "pp_test_seconds_count 4" in text


def test_label_mismatch_raises():
    c = m.Counter("pp_test_lbl_total", "help", ["a", "b"])
    with pytest.raises(ValueError):
        c.inc(a="1")  # missing b


def test_global_render_includes_registered():
    m.Counter("pp_unique_marker_total", "help").inc()
    assert "pp_unique_marker_total" in m.render()
