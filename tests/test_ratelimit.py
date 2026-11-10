from ratelimit import RateLimiter


def test_per_client_limit_and_window():
    t = [0.0]
    rl = RateLimiter(per_client=2, global_limit=0, window_s=10, clock=lambda: t[0])
    assert rl.allow("a") and rl.allow("a")
    assert not rl.allow("a")
    assert rl.allow("b")          # other visitors unaffected
    t[0] = 11
    assert rl.allow("a")          # window expired


def test_global_limit():
    rl = RateLimiter(per_client=0, global_limit=2, window_s=10, clock=lambda: 0.0)
    assert rl.allow("a") and rl.allow("b")
    assert not rl.allow("c")
